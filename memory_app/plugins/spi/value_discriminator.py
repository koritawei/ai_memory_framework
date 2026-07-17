"""ValueDiscriminator SPI —— 价值判别（设计文档 §5.1.5.10）。

判定 MemCell 是否值得触发画像抽取 / 高成本 LLM 路径。
默认实现 ``llm_scenario_discriminator`` —— LLM 二分判定 + 置信度 + 推理理由。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel

from memory_app.internal_models import MemCell
from memory_app.plugins.base import Plugin


class ValueJudgement(BaseModel):
    """价值判别结果。"""

    is_high_value: bool
    confidence: float = 0.0  # [0, 1]
    reasoning: str = ""


class ValueDiscriminator(Plugin):
    """价值判别扩展点。"""

    @abstractmethod
    async def is_high_value(
        self, memcell: MemCell, recent_context: list[MemCell] | None = None
    ) -> ValueJudgement:
        """判定 memcell 是否高价值。

        约定：
        - ``recent_context`` 是滚动窗口内最近 N 条 MemCell（用以让 LLM 看到上下文）
        - 判定阈值由实现内部决定（默认 ``min_confidence=0.6``）
        - LLM 调用应 ``temperature=0.0`` 保证确定性
        - 调用方仅在 ``is_high_value=True && confidence ≥ min_confidence`` 时
          才触发后续高成本路径（如 ProfileExtractor）
        """


__all__ = ["ValueDiscriminator", "ValueJudgement"]
