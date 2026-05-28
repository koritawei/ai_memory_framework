"""PluginFactory —— 串联 :class:`PluginRegistry` 与
:class:`memory_app.config_center.ConfigCenter`。

═══════════════════════════════════════════════════════════════════════════════
为什么需要 Factory
═══════════════════════════════════════════════════════════════════════════════
业务平面只有「我要 boundary_detector」这种意图，并不知道：
- 当前应该用 ``rule_sbd`` 还是 ``hybrid_sbd``（由 ConfigCenter 决定）
- ``time_gap_min`` 当前是 30 还是 45（由 ConfigCenter 决定）
- 该实例是 acme tenant 专属还是全局共享（由 ConfigCenter 决定）
- 是否应该重新构造（由 ConfigCenter 的版本号决定）

Factory 把这些决策集中起来，对业务平面只暴露一句：

.. code-block:: python

    sbd = await factory.build("memory.generation.boundary_detector",
                               tenant_id, user_id)

═══════════════════════════════════════════════════════════════════════════════
缓存策略
═══════════════════════════════════════════════════════════════════════════════
按 ``(category, name, tenant_id, version)`` 四元组缓存实例。其中：

- ``tenant_id`` 进入 key：不同租户即使 name 相同也各自独立实例（隔离配置变更）
- ``version`` 进入 key：ConfigCenter 配置变更后 ``version`` 自动 bump，
  下次 ``build`` 自然产出新 key → 触发新建实例

═══════════════════════════════════════════════════════════════════════════════
配置变更触发的 reload
═══════════════════════════════════════════════════════════════════════════════
``attach_config_center`` 注册了 watcher 回调：当 ConfigCenter 检测到配置变更，
受影响 category 的所有缓存实例 ``stop`` 后丢弃；下次 ``build`` 自动重建。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from .base import Plugin, PluginError, PluginErrorCategory
from .registry import PluginRegistry, registry as default_registry

if TYPE_CHECKING:  # 仅用于类型提示，运行时不导入避免循环依赖
    from memory_app.config_center.base import ConfigCenter, ConfigChangeEvent

logger = logging.getLogger(__name__)


class PluginFactory:
    """按 ``(category, tenant_id, user_id, version)`` 缓存已 start 的插件实例。"""

    def __init__(
        self,
        registry: PluginRegistry | None = None,
        config_center: "ConfigCenter | None" = None,
    ) -> None:
        # 默认走全局 registry，便于绝大多数生产路径
        self._registry = registry or default_registry
        self._config = config_center
        # 缓存 key: (category, name, tenant_id_or_*, config_version)
        self._instances: dict[tuple[str, str, str, int], Plugin] = {}
        # 性能:按 cache_key 分锁 —— 不同 (category, tenant, version) 的 build
        # 可以并发进行;原全局 ``_lock`` 让 ``factory.build("fuser")`` 等待
        # ``factory.build("reranker")`` 的 ``start`` 完成,启动期串行 N 倍延迟。
        # ``setdefault`` 在 CPython 是 GIL-原子的;多并发同 key 时偶发多创建一个
        # Lock 是安全的(下次同 key 命中 instance cache fast-path,新 lock 永不再用)
        self._build_locks: dict[tuple[str, str, str, int], asyncio.Lock] = {}
        # 防止重复挂 watcher（多次 attach 同一 ConfigCenter）
        self._watcher_attached = False

    # ════════════════════════════════════════════════════════════════════════
    # 主入口
    # ════════════════════════════════════════════════════════════════════════
    async def build(
        self,
        category: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_override: Optional[dict] = None,
    ) -> Plugin:
        """按当前配置构造（或复用缓存的）插件实例。

        :param category: SPI category，如 ``"memory.generation.boundary_detector"``
        :param tenant_id: 租户隔离用；不同租户共享 SPI 但配置可独立
        :param user_id: 用户隔离用；通常仅参与灰度路由，不进缓存 key
        :param request_override: 仅本次调用生效的临时覆盖（受白名单约束）
        :raises PluginError: ConfigCenter 未挂接或 ``start`` 失败
        """
        if self._config is None:
            raise PluginError(
                PluginErrorCategory.CONFIG,
                "config_center_unset",
                "PluginFactory.config_center is None; call attach_config_center first",
            )
        cfg = await self._config.resolve(
            category, tenant_id=tenant_id, user_id=user_id, request_override=request_override
        )
        # request_override 是"仅本次调用生效"的语义:必须 bypass cache,
        # 否则首个传 override-A 的请求把实例缓存进去,后续不传 override
        # (或传 override-B)的请求会拿到带 A 参数的旧实例 —— 静默语义错误。
        # 临时实例 start 后立即销毁,不进 _instances。
        bypass_cache = request_override is not None
        if bypass_cache:
            cls = self._registry.get(category, cfg.name)
            instance = cls()
            try:
                await instance.start(cfg.params)
            except Exception as e:  # noqa: BLE001
                raise PluginError(
                    PluginErrorCategory.INTERNAL,
                    "plugin_start_failed",
                    f"{category}/{cfg.name} start failed (request_override path): {e}",
                    cause=e,
                ) from e
            logger.debug(
                "plugin built ephemerally (request_override): %s/%s tenant=%s",
                category, cfg.name, tenant_id,
            )
            return instance
        cache_key = (category, cfg.name, tenant_id or "*", cfg.version)
        if cache_key in self._instances:
            return self._instances[cache_key]
        # double-checked locking + per-key 锁:不同 key 的 build 互相并发
        lock = self._build_locks.setdefault(cache_key, asyncio.Lock())
        try:
            async with lock:
                if cache_key in self._instances:
                    return self._instances[cache_key]
                cls = self._registry.get(category, cfg.name)
                instance = cls()
                try:
                    await instance.start(cfg.params)
                except Exception as e:  # noqa: BLE001
                    # 包装为 PluginError 让框架按"内部错误"处理
                    raise PluginError(
                        PluginErrorCategory.INTERNAL,
                        "plugin_start_failed",
                        f"{category}/{cfg.name} start failed: {e}",
                        cause=e,
                    ) from e
                self._instances[cache_key] = instance
                logger.info(
                    "plugin started: %s/%s tenant=%s version=%d",
                    category, cfg.name, tenant_id, cfg.version,
                )
                return instance
        finally:
            # 无论成功还是 start 失败,都从 dict 清掉本 key 的 lock,避免
            # 反复重试导致 _build_locks 无界增长(P3.1 引入,P3.1-bugfix)。
            # 已 acquire 同一 lock 的并发 coro 仍能继续——它们持有的是 Lock 对象
            # 引用,不依赖它在 dict 里的存在。同时:
            # - 成功 build 后该 key 永远走 fast-path(if cache_key in _instances),
            #   新建 Lock 不会被 await,只是 dict 短暂入项再被 GC,无害
            # - 失败 build 下次重试 setdefault 创建全新 Lock,旧 Lock 被 GC
            self._build_locks.pop(cache_key, None)

    # ════════════════════════════════════════════════════════════════════════
    # 配置变更触发重建
    # ════════════════════════════════════════════════════════════════════════
    async def attach_config_center(self, config_center: "ConfigCenter") -> None:
        """挂接 ConfigCenter，自动订阅配置变更触发实例 reload。

        多次调用幂等 —— 内部 ``_watcher_attached`` 标志位防止重复 watch。
        """
        self._config = config_center
        if not self._watcher_attached:
            await config_center.watch(self._on_config_change)
            self._watcher_attached = True

    async def _on_config_change(self, event: "ConfigChangeEvent") -> None:
        """ConfigCenter watcher 回调：丢弃受影响 category 的缓存实例。

        策略：**丢弃 + 下次重建** 而不是 ``reload`` —— 实现简单，
        且能正确处理 ``name`` 切换（如 ``rule_sbd`` → ``hybrid_sbd``）。
        ``event.category == "*"`` 时表示全局变更（如 YAML 整体重载），全部丢弃。
        """
        affected = [
            key for key in self._instances.keys() if event.category in ("*", key[0])
        ]
        for key in affected:
            inst = self._instances.pop(key, None)
            if inst is None:
                continue
            try:
                await inst.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("plugin stop on reload failed: %s/%s: %s", key[0], key[1], e)
        if affected:
            logger.info(
                "config change %s → %d plugin instance(s) released",
                event.category, len(affected),
            )

    async def release_category(
        self, category: str, name: str | None = None
    ) -> int:
        """手工释放某 category(可选指定 name)下所有活动实例,下次 build 重建。

        管理面:供 ``POST /v1/admin/plugins/{category}/{name}/reload``
        使用,作为配置中心暂时不可达 / 灰度回滚兜底。

        :returns: 实际被 stop+丢弃的实例个数
        """
        affected_keys = [
            key for key in self._instances.keys()
            if key[0] == category and (name is None or key[1] == name)
        ]
        for key in affected_keys:
            inst = self._instances.pop(key, None)
            if inst is None:
                continue
            try:
                await inst.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "plugin manual reload stop failed: %s/%s: %s",
                    key[0], key[1], e,
                )
        if affected_keys:
            logger.info(
                "manual reload: released %d instance(s) of %s/%s",
                len(affected_keys), category, name or "*",
            )
        return len(affected_keys)

    async def health_of(self, category: str, name: str) -> dict:
        """单插件实例健康(取第一个匹配的活动实例)。

        管理面:供 ``GET /v1/admin/plugins/{category}/{name}/health``。
        """
        for key, inst in self._instances.items():
            if key[0] == category and key[1] == name:
                try:
                    return await inst.health()
                except Exception as e:  # noqa: BLE001
                    return {"status": "fail", "detail": str(e)}
        return {"status": "not_active", "detail": "no active instance"}

    # ════════════════════════════════════════════════════════════════════════
    # 关闭
    # ════════════════════════════════════════════════════════════════════════
    async def shutdown(self) -> None:
        """优雅停止所有缓存实例。FastAPI lifespan close 阶段调用。"""
        for key, inst in list(self._instances.items()):
            try:
                await inst.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("plugin %s/%s stop failed during shutdown: %s", key[0], key[1], e)
        self._instances.clear()

    # ════════════════════════════════════════════════════════════════════════
    # 观测
    # ════════════════════════════════════════════════════════════════════════
    def list_active(self) -> list[dict]:
        """列出当前所有活动实例的元信息（供 Admin API 返回）。"""
        return [
            {
                "category": k[0],
                "name": k[1],
                "tenant_id": k[2],
                "config_version": k[3],
            }
            for k in self._instances.keys()
        ]

    async def healthcheck_all(self) -> dict[str, dict]:
        """聚合所有活动实例的健康状态（供 ``/v1/admin/plugins/health``）。"""
        out: dict[str, dict] = {}
        for key, inst in self._instances.items():
            slug = f"{key[0]}/{key[1]}"
            try:
                out[slug] = await inst.health()
            except Exception as e:  # noqa: BLE001
                # health 异常不应阻断整体健康检查，统一标 fail
                out[slug] = {"status": "fail", "detail": str(e)}
        return out


__all__ = ["PluginFactory"]
