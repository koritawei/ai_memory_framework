"""Clusterer SPI —— MemScene 增量聚类。

默认实现 ``incremental_centroid``：质心法 + 余弦相似度 + 时间窗。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel

from memory_app.internal_models import MemCell
from memory_app.plugins.base import Plugin


class ClusterAssignmentMeta(BaseModel):
    """单次聚类决策的元信息（便于排查与监控）。"""

    similarity: float = 0.0  # 与最近簇质心的余弦相似度
    is_new_cluster: bool = False  # 是否新建了簇
    pre_centroid_distance: float | None = None  # 预合并距离（如有）


class Clusterer(Plugin):
    """聚类扩展点。"""

    @abstractmethod
    async def cluster(
        self, group_id: str, memcell: MemCell
    ) -> tuple[str, ClusterAssignmentMeta]:
        """把 MemCell 归入一个簇，返回 ``(cluster_id, meta)``。

        约定：
        - 实现应做**增量**聚类（不重算所有历史质心），保证 O(N_clusters) 复杂度
        - 当语义相似度 < 阈值（默认 0.65）或时间间隔 > 窗口（默认 7d）→ 新建簇
        - 幂等性：同一 ``memcell.mem_cell_id`` 多次提交，应分配同一簇
        - 实现内部异常应包装为 :class:`PluginError(category="internal", retryable=True)`
        """


__all__ = ["Clusterer", "ClusterAssignmentMeta"]
