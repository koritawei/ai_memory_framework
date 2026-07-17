"""``incremental_centroid`` —— Phase 3 Step 3.4 增量质心聚类插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.clusterer.Clusterer` 的默认实现。
内部委托 :class:`memory_app.clustering.ClusterManager` 的纯算法,自身负责:
- ``start(config)`` 解析阈值
- 维护 "(tenant_id, user_id, group_id) → list[MemScene]" 的内存索引
- 把决策结果转 SPI ``(cluster_id, ClusterAssignmentMeta)``

═══════════════════════════════════════════════════════════════════════════════
为什么 scenes 索引在内存
═══════════════════════════════════════════════════════════════════════════════
- Phase 3 简化:实例每个 group_id 维护一个滑动窗口的 scenes(LRU 上限 256)
- 进程重启后从 KVStore 拉回(Phase 6+);本插件**不**在 start 内做加载
- 高 QPS 场景考虑切到 Redis sorted set / 持久化 collection

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    similarity_threshold: float 默认 0.65
    time_gap_days:        float 默认 7
    max_scene_size:       int   默认 50
    max_scenes_per_group: int   默认 256  (LRU 上限,Phase 3 简化)
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any, Mapping

from memory_app.clustering import (
    ClusterManager,
    ClusterManagerConfig,
    parse_cluster_config,
)
from memory_app.internal_models import MemCell, MemScene
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.clusterer import ClusterAssignmentMeta, Clusterer

logger = logging.getLogger(__name__)


@register
class IncrementalCentroidClusterer(Clusterer):
    """质心法 + 余弦相似度 + 时间窗的增量聚类。"""

    meta = PluginMeta(
        name="incremental_centroid",
        category="memory.generation.clusterer",
        version="1.0.0",
        description="增量质心聚类(余弦 + 时间窗 + 容量上限)",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "similarity_threshold": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.65
                },
                "time_gap_days": {
                    "type": "number", "minimum": 0.0, "maximum": 365, "default": 7
                },
                "max_scene_size": {
                    "type": "integer", "minimum": 1, "default": 50
                },
                "max_scenes_per_group": {
                    "type": "integer", "minimum": 16, "maximum": 4096, "default": 256
                },
            },
        },
    )

    def __init__(self) -> None:
        self._config: ClusterManagerConfig = ClusterManagerConfig()
        self._max_scenes_per_group: int = 256
        self._manager: ClusterManager = ClusterManager(self._config)
        # group_key → OrderedDict[scene_id, MemScene]   (LRU 顺序)
        self._scenes_by_group: dict[str, "OrderedDict[str, MemScene]"] = {}
        # 每 group_key 一把锁:串行化"读 existing → 决策 → 写 bucket"。
        # 否则两个并发 cluster() 都看到同一份 existing,都决定新建 scene,
        # 同一时刻同一 group_key 的 scene 数翻倍,聚类不变量被破坏。
        # 用 OrderedDict 实现 LRU,防止高基数租户/group 让锁数无限增长。
        self._group_locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
        # 锁池上限:典型 group 数 << 4×_max_scenes_per_group,留足容量。
        # 注意 lock 本身只在被 acquire 时持引用,evict 不安全 —— 我们只在
        # 锁**未持有**时 popitem,通过 ``acquire 失败重试`` 简化为"锁池满
        # 时拒绝 evict 持有中的锁,只 evict 队首已释放的"。
        self._group_locks_cap: int = 1024
        # 分配 group 锁本身也需要互斥(否则两 coro 同时 setdefault 拿到不同对象)
        self._group_locks_guard: asyncio.Lock = asyncio.Lock()

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        cfg = dict(config)
        self._config = parse_cluster_config(cfg)
        self._max_scenes_per_group = int(cfg.get("max_scenes_per_group", 256))
        self._manager = ClusterManager(self._config)
        # 不清掉已有 scenes:reload 时希望保留近期判定结果
        logger.info(
            "incremental_centroid started: sim>=%.2f, time<=%s, max_size=%d, lru=%d",
            self._config.similarity_threshold, self._config.time_gap_max,
            self._config.max_scene_size, self._max_scenes_per_group,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        total_groups = len(self._scenes_by_group)
        total_scenes = sum(len(g) for g in self._scenes_by_group.values())
        return {
            "status": "ok",
            "detail": f"groups={total_groups}, scenes={total_scenes}",
        }

    async def metrics(self) -> dict:
        return {
            "incremental_centroid_groups": len(self._scenes_by_group),
            "incremental_centroid_scenes": sum(
                len(g) for g in self._scenes_by_group.values()
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # SPI: cluster
    # ────────────────────────────────────────────────────────────────────────
    async def cluster(
        self, group_id: str, memcell: MemCell
    ) -> tuple[str, ClusterAssignmentMeta]:
        """SPI 契约:把 ``memcell`` 归入一簇,返回 ``(cluster_id, meta)``。

        - ``cluster_id`` 取 ``MemScene.scene_id`` —— 同一记忆的 scene 即 cluster
        - 实例内部按 ``group_id`` 隔离 scenes 列表;``group_id`` 实际取调用方
          指定值或 cell.session_id / cell.group_id
        - 内存 LRU 上限 :attr:`_max_scenes_per_group`,超出弹出最早 scene
        """
        try:
            return await self._cluster_inner(group_id, memcell)
        except Exception as e:  # noqa: BLE001
            raise PluginError(
                PluginErrorCategory.INTERNAL,
                "cluster_failed",
                f"incremental_centroid failed: {e}",
                retryable=True,
                cause=e,
            ) from e

    async def _cluster_inner(
        self, group_id: str, memcell: MemCell
    ) -> tuple[str, ClusterAssignmentMeta]:
        key = self._group_key(group_id, memcell)
        lock = await self._get_group_lock(key)
        async with lock:
            bucket = self._scenes_by_group.setdefault(key, OrderedDict())
            existing = list(bucket.values())
            decision = self._manager.assign(memcell, existing)
            scene = decision.scene

            if scene.scene_id in bucket:
                # 命中已有 scene → LRU 顶到末尾
                bucket.move_to_end(scene.scene_id)
            else:
                bucket[scene.scene_id] = scene
                # LRU 弹出
                while len(bucket) > self._max_scenes_per_group:
                    evicted_id, _ = bucket.popitem(last=False)
                    logger.debug("LRU evict scene %s from group=%s", evicted_id, key)

            meta = ClusterAssignmentMeta(
                similarity=decision.similarity,
                is_new_cluster=decision.is_new_cluster,
            )
            return scene.scene_id, meta

    async def _get_group_lock(self, key: str) -> asyncio.Lock:
        """懒分配 group 级锁;LRU 上限防止高基数下内存泄漏。

        - 命中:move_to_end 标记最近使用
        - 未命中:创建后插入末尾;若超过 ``_group_locks_cap``,从队首 evict 一把
          未被持有的锁(被持有则 skip,留给下次 evict 处理)
        """
        lock = self._group_locks.get(key)
        if lock is not None:
            # 已存在 → LRU 顶到末尾
            try:
                self._group_locks.move_to_end(key)
            except KeyError:
                pass
            return lock
        async with self._group_locks_guard:
            lock = self._group_locks.get(key)
            if lock is not None:
                try:
                    self._group_locks.move_to_end(key)
                except KeyError:
                    pass
                return lock
            lock = asyncio.Lock()
            self._group_locks[key] = lock
            # 超容时 evict 最老且未被持有的锁;最多扫描一遍,避免 O(N) 死循环
            if len(self._group_locks) > self._group_locks_cap:
                for old_key in list(self._group_locks.keys()):
                    if old_key == key:
                        continue
                    old_lock = self._group_locks[old_key]
                    if not old_lock.locked():
                        self._group_locks.pop(old_key, None)
                        break
            return lock

    # ────────────────────────────────────────────────────────────────────────
    # 测试 / 调试便利方法(非 SPI 契约;工具脚本可直接读)
    # ────────────────────────────────────────────────────────────────────────
    def get_scenes(self, group_id: str, tenant_id: str, user_id: str) -> list[MemScene]:
        key = self._make_key(tenant_id, user_id, group_id)
        return list(self._scenes_by_group.get(key, OrderedDict()).values())

    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _group_key(group_id: str, memcell: MemCell) -> str:
        gid = group_id or memcell.group_id or memcell.session_id or "default"
        return IncrementalCentroidClusterer._make_key(memcell.tenant_id, memcell.user_id, gid)

    @staticmethod
    def _make_key(tenant_id: str, user_id: str, group_id: str) -> str:
        return f"{tenant_id}::{user_id}::{group_id}"


__all__ = ["IncrementalCentroidClusterer"]
