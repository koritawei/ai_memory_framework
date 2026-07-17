"""``threshold`` —— Phase 4 Step 4.4 阈值过滤插件。

═══════════════════════════════════════════════════════════════════════════════
策略
═══════════════════════════════════════════════════════════════════════════════
- 剔除 ``score < threshold`` 的候选(默认 0.55)
- 不修改候选,仅返回子集且保留原顺序

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    threshold: float 默认 0.55
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.internal_models import RankedMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.plugins.spi.retrieval_filter import RetrievalFilter

logger = logging.getLogger(__name__)


@register
class ThresholdFilter(RetrievalFilter):
    """阈值过滤(Phase 4 默认)。"""

    meta = PluginMeta(
        name="threshold",
        category="memory.retrieval.filter",
        version="1.0.0",
        description="按 score 阈值过滤(默认 0.55)",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "threshold": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.55
                },
            },
        },
    )

    def __init__(self) -> None:
        self._threshold: float = 0.55

    async def start(self, config: Mapping[str, Any]) -> None:
        try:
            self._threshold = max(0.0, min(1.0, float(config.get("threshold", 0.55))))
        except (TypeError, ValueError):
            self._threshold = 0.55
        logger.info("threshold filter started: threshold=%.3f", self._threshold)

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {"status": "ok", "detail": f"threshold={self._threshold:.3f}"}

    async def filter(
        self, candidates: list[RankedMemory], ctx: RetrievalContext
    ) -> list[RankedMemory]:
        if not candidates:
            return []
        return [c for c in candidates if float(c.score) >= self._threshold]


__all__ = ["ThresholdFilter"]
