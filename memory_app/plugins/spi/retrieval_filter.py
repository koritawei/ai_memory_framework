"""RetrievalFilter SPI —— 检索过滤（设计文档 §6.4）。

链式调用：threshold → lifecycle → quota。
默认实现：
- ``threshold_filter``  剔除 score < 0.55 的候选
- ``lifecycle_filter``  剔除 ``ARCHIVED`` 状态的记忆（Phase 2+）
- ``quota_filter``      按查询意图调整情景/语义比例
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.internal_models import RankedMemory
from memory_app.plugins.base import Plugin
from .retrieval_channel import RetrievalContext


class RetrievalFilter(Plugin):
    """检索过滤扩展点。"""

    @abstractmethod
    async def filter(
        self, candidates: list[RankedMemory], ctx: RetrievalContext
    ) -> list[RankedMemory]:
        """过滤候选集。

        约定：
        - 返回的列表是 candidates 的子集且保持原顺序
        - 不应修改候选项（只能丢弃）
        - 单个过滤器应职责单一（threshold / lifecycle / quota 拆开实现，
          orchestrator 自己组合）
        - 实现应是确定性的：相同输入应总是返回相同输出
        """


__all__ = ["RetrievalFilter"]
