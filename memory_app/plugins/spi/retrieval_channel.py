"""RetrievalChannel SPI —— 单路召回通道（设计文档 §6.0–§6.5）。

四个默认实现（按优先级）：
- ``vector_milvus``  向量语义（含 Fisher-Rao 模式）
- ``bm25_es``        BM25 关键词
- ``entity_boost``   Entity Store 反向索引（Phase 3）
- ``graph_traversal`` 图遍历（Phase 3）
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Iterable

from pydantic import BaseModel, ConfigDict

from memory_app.internal_models import RankedMemory
from memory_app.plugins.base import Plugin


class RetrievalContext(BaseModel):
    """检索调用上下文。"""

    model_config = ConfigDict(extra="allow")

    tenant_id: str
    user_id: str
    intent: str | None = None  # factual / opinion / temporal / multi_hop
    filters: dict | None = None
    enabled_memory_types: list[str] | None = None  # ["EPISODIC", "SEMANTIC"]


class RetrievalChannel(Plugin):
    """单路召回通道扩展点。"""

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """通道短名称，用于 RRF 加权 / 监控指标 label。

        约定：返回值应在通道生命周期内不变（不要从 config 动态读）。
        """

    @abstractmethod
    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]:
        """在本通道内召回 Top-k。

        约定：
        - 返回的 ``RankedMemory.source_channel`` 应等于 ``self.channel_name``
        - 返回列表按 ``score`` 降序，``rank`` 从 0 开始填充
        - 实现可做"过取"：内部召回 ``max(k*4, 100)`` 后再返 Top-k，提升 RRF 质量
        - 通道不可用时（如 ES 宕机）应抛 :class:`PluginError(category="dependency",
          retryable=True)` 让上游进入降级路径，**不要返回空列表**（无法区分"真无结果"）
        """


__all__ = ["RetrievalChannel", "RetrievalContext"]
