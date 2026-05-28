"""ClusterManager —— MemScene 增量聚类核心算法。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
本模块承载 "纯算法 / 无外部连接" 的增量质心聚类。插件层
:class:`memory_app.plugins_default.incremental_centroid.IncrementalCentroidClusterer`
是薄包装,负责满足 :class:`Clusterer` SPI(start / stop + cluster)。

═══════════════════════════════════════════════════════════════════════════════
策略
═══════════════════════════════════════════════════════════════════════════════
1. 输入新的 ``MemCell``,有 ``embedding`` 才能聚类(无 embedding → 单独成簇)
2. 在 "已有 scenes" 中找余弦相似度最高的 scene
3. 满足三条件即合并(``∧``):
   - ``similarity ≥ similarity_threshold``(默认 0.65)
   - ``time_gap ≤ time_gap_max``(默认 7d)
   - ``len(member_ids) < max_scene_size``(默认 50)
4. 否则创建新 scene

═══════════════════════════════════════════════════════════════════════════════
增量更新质心
═══════════════════════════════════════════════════════════════════════════════
::

    n = len(members)        # 加入新 cell 后的总数
    centroid = (centroid * (n - 1) + new_emb) / n

复杂度 O(N_clusters),与现有规模线性,不重算历史向量。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from memory_app._compat import utcnow
from typing import Any

from memory_app.internal_models import MemCell, MemScene

logger = logging.getLogger(__name__)

# numpy 用于 assign 时批量算 cosine;无 numpy 时 fallback 到纯 Python 循环
try:
    import numpy as _np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    _np = None  # type: ignore[assignment]
    _HAS_NUMPY = False


# ════════════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ClusterManagerConfig:
    """聚类参数。

    所有阈值都可由 ConfigCenter 下发(见 plugins_default/incremental_centroid.py)。
    """

    #: 余弦相似度阈值,< 此值即新建簇
    similarity_threshold: float = 0.65

    #: 时间窗,scene.last_updated_at 与新 cell.timestamp 间隔 > 此值即新建簇
    time_gap_max: timedelta = timedelta(days=7)

    #: 单个 scene 最多 member 数,达上限即新建
    max_scene_size: int = 50


def parse_cluster_config(params: dict[str, Any] | None) -> ClusterManagerConfig:
    """从插件参数 dict 构造配置。

    支持字段(全部可选):
        - ``similarity_threshold`` (float)
        - ``time_gap_days``        (int / float, 转 timedelta)
        - ``max_scene_size``       (int)
    """
    cfg = ClusterManagerConfig()
    if not params:
        return cfg
    if "similarity_threshold" in params:
        cfg.similarity_threshold = float(params["similarity_threshold"])
    if "time_gap_days" in params:
        cfg.time_gap_max = timedelta(days=float(params["time_gap_days"]))
    if "max_scene_size" in params:
        cfg.max_scene_size = int(params["max_scene_size"])
    return cfg


# ════════════════════════════════════════════════════════════════════════════
# 决策结果
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ClusterAssignment:
    """聚类决策的输出,供插件层转 SPI ``ClusterAssignmentMeta``。"""

    scene: MemScene
    similarity: float = 0.0
    is_new_cluster: bool = False


# ════════════════════════════════════════════════════════════════════════════
# 核心
# ════════════════════════════════════════════════════════════════════════════
class ClusterManager:
    """增量质心聚类器。

    本类**无副作用**:不做持久化、不维护"全局 scenes 字典";调用方负责把
    ``existing_scenes`` 列表喂进来,并将返回的 ``ClusterAssignment.scene``
    保存回数据库。

    幂等性由调用方保证(若同一 ``mem_cell_id`` 已经在 ``existing_scenes``
    某 scene 内,本类**不会**重复加入)。
    """

    def __init__(self, config: ClusterManagerConfig | None = None) -> None:
        self.config = config or ClusterManagerConfig()

    # ────────────────────────────────────────────────────────────────────────
    # 主入口
    # ────────────────────────────────────────────────────────────────────────
    def assign(
        self, cell: MemCell, existing_scenes: list[MemScene]
    ) -> ClusterAssignment:
        """决策:把 ``cell`` 归入最佳已有 scene 或创建新 scene。

        语义:
        - ``cell`` 已经在某 scene 内 → 返回该 scene(幂等)
        - ``cell.embedding`` 为空 → 创建新 scene(无法计算相似度)
        - 任意已有 scene 满足三条件(sim, time, size) → 合并 + 增量更新质心
        - 否则创建新 scene
        """
        # 幂等:已有归属直接返回
        for sc in existing_scenes:
            if cell.mem_cell_id in sc.member_episode_ids:
                return ClusterAssignment(scene=sc, similarity=1.0, is_new_cluster=False)

        if not cell.embedding:
            return ClusterAssignment(
                scene=self._create_new_scene(cell), similarity=0.0, is_new_cluster=True
            )

        # 先按"非相似度"约束过滤(size / time window / 有 centroid),只把候选
        # 喂给 numpy 算一次矩阵乘 —— 把 N 次 Python cosine_similarity 降为
        # 1 次 numpy dot,N 量级在数十到几百时收益显著。
        candidates: list[MemScene] = []
        for sc in existing_scenes:
            if not sc.centroid:
                continue
            if len(sc.member_episode_ids) >= self.config.max_scene_size:
                continue
            if not self._within_time_window(sc, cell):
                continue
            if len(sc.centroid) != len(cell.embedding):
                continue
            candidates.append(sc)

        if not candidates:
            return ClusterAssignment(
                scene=self._create_new_scene(cell), similarity=0.0, is_new_cluster=True
            )

        sims = self._batch_cosine(cell.embedding, [c.centroid for c in candidates])
        best: MemScene | None = None
        best_sim = 0.0
        for sc, sim in zip(candidates, sims):
            if sim < self.config.similarity_threshold:
                continue
            if sim > best_sim:
                best = sc
                best_sim = sim

        if best is None:
            return ClusterAssignment(
                scene=self._create_new_scene(cell), similarity=best_sim, is_new_cluster=True
            )
        self._merge_into(best, cell)
        return ClusterAssignment(scene=best, similarity=best_sim, is_new_cluster=False)

    # ────────────────────────────────────────────────────────────────────────
    # 内部:批量 cosine
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _batch_cosine(
        query_vec: list[float], centroids: list[list[float]]
    ) -> list[float]:
        """计算 ``query_vec`` 与 ``centroids`` 中每条向量的 cosine 相似度。

        numpy 可用时 1 次矩阵乘搞定 N 条;否则退化到逐条 Python cosine。
        """
        if not centroids:
            return []
        if _HAS_NUMPY:
            q = _np.asarray(query_vec, dtype=_np.float32)
            qn = float(_np.linalg.norm(q))
            if qn == 0.0:
                return [0.0] * len(centroids)
            mat = _np.asarray(centroids, dtype=_np.float32)
            mn = _np.linalg.norm(mat, axis=1)
            # 分母 0 的行 sim=0;用 where 避免除零警告
            denom = mn * qn
            with _np.errstate(invalid="ignore", divide="ignore"):
                raw = mat @ q / denom
            raw = _np.where(denom > 0, raw, 0.0)
            return [float(x) for x in raw]
        # fallback
        return [cosine_similarity(query_vec, c) for c in centroids]

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    def _within_time_window(self, scene: MemScene, cell: MemCell) -> bool:
        last = scene.last_updated_at or scene.created_at
        if last is None:
            return True
        cell_t = cell.timestamp or cell.created_at
        if cell_t is None:
            return True
        last = _normalize_time(last)
        cell_t = _normalize_time(cell_t)
        return abs(cell_t - last) <= self.config.time_gap_max

    def _merge_into(self, scene: MemScene, cell: MemCell) -> None:
        """合并 cell 进 scene + 增量更新质心 / 计数 / 时间。"""
        scene.member_episode_ids.append(cell.mem_cell_id)
        scene.member_count = len(scene.member_episode_ids)
        scene.centroid = update_centroid(
            scene.centroid or [], cell.embedding or [], scene.member_count
        )
        scene.last_updated_at = _normalize_time(cell.timestamp or utcnow())

    def _create_new_scene(self, cell: MemCell) -> MemScene:
        ts = _normalize_time(cell.timestamp or utcnow())
        return MemScene(
            tenant_id=cell.tenant_id,
            user_id=cell.user_id,
            group_id=cell.group_id,
            centroid=list(cell.embedding) if cell.embedding else None,
            member_episode_ids=[cell.mem_cell_id],
            member_count=1,
            pending_semantic_digest=True,
            created_at=ts,
            last_updated_at=ts,
        )


# ════════════════════════════════════════════════════════════════════════════
# 数学工具(纯函数,可独立单测)
# ════════════════════════════════════════════════════════════════════════════
def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度。空向量 / 维度不一致 / 零向量 → 返回 0.0。

    实现:不依赖 numpy,纯 Python(本算法热路径调用频次低,sklearn 太重)。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def update_centroid(
    old_centroid: list[float], new_emb: list[float], new_size: int
) -> list[float]:
    """质心增量更新。

    ::

        centroid_new = (centroid_old * (n - 1) + new_emb) / n

    边界:
    - ``new_size <= 0`` → 返回 ``new_emb``(初始化)
    - ``old_centroid`` 为空 → 返回 ``new_emb``(首个成员)
    - ``old_centroid`` 维度与 ``new_emb`` 不一致 → 直接返回 ``new_emb``(降级)
    - ``new_size == 1`` 且 ``old_centroid`` 非空:正确的均值公式
      ``(old * 0 + new) / 1 = new``,继续走通用公式即可;无须特判。
    """
    if new_size <= 0 or not old_centroid:
        return list(new_emb)
    if len(old_centroid) != len(new_emb):
        return list(new_emb)
    n = float(new_size)
    return [(old * (n - 1.0) + new) / n for old, new in zip(old_centroid, new_emb)]


def _normalize_time(t: datetime) -> datetime:
    """把 naive datetime 视为 UTC,避免 tz-aware 比较抛 TypeError。"""
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t


__all__ = [
    "ClusterManagerConfig",
    "parse_cluster_config",
    "ClusterAssignment",
    "ClusterManager",
    "cosine_similarity",
    "update_centroid",
]
