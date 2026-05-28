"""Consolidator SPI —— 语义事实冲突消解四操作。

默认实现 ``composite_consolidator`` = 规则 + Sheaf Cohomology + LLM 兜底。
四操作决策：ADD / UPDATE / SUPERSEDE / NOOP。
"""

from __future__ import annotations

from abc import abstractmethod
from enum import Enum

from pydantic import BaseModel

from memory_app.internal_models import SemanticMemory
from memory_app.plugins.base import Plugin


class ConsolidationDecision(str, Enum):
    """消解决策。"""

    ADD = "ADD"            # 新事实，无冲突 → 直接存储
    UPDATE = "UPDATE"      # 高相似 + 信息互补 → 合并
    SUPERSEDE = "SUPERSEDE"  # 高相似 + 矛盾 → 替代旧事实（旧的标记 is_valid=false）
    NOOP = "NOOP"          # 完全重复 → 跳过


class ConsolidatorResult(BaseModel):
    """消解决策结果。"""

    decision: ConsolidationDecision
    target_id: str | None = None  # UPDATE / SUPERSEDE 时为被影响的旧记忆 ID
    composite_sim: float = 0.0
    reasoning: str = ""


class Consolidator(Plugin):
    """语义事实冲突消解扩展点。"""

    @abstractmethod
    async def consolidate(
        self,
        new_fact: SemanticMemory,
        existing_facts: list[SemanticMemory],
    ) -> ConsolidatorResult:
        """决定如何把 new_fact 写入到已有 fact 集合。

        约定：
        - ``existing_facts`` 应已按相似度预筛选（实现可信任）
        - 综合相似度 = 0.4 × Jaccard(entities) + 0.6 × Cosine(embedding)
        - 阈值：sim < 0.85 → ADD；0.85 ≤ sim < 0.95 → UPDATE/SUPERSEDE（看矛盾）；
          sim ≥ 0.95 → NOOP
        - 矛盾检测应先用纯数学方法（Sheaf Cohomology），LLM 仅作兜底
        - 单次决策延迟应 < 100ms（含 LLM 兜底则 < 2s）
        """


__all__ = ["Consolidator", "ConsolidationDecision", "ConsolidatorResult"]
