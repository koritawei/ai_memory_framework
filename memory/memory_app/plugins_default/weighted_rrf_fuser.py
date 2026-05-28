"""``weighted_rrf`` —— 检索 RRF 融合插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.fuser.Fuser` 的默认实现。委托
:class:`memory_app.retrieval.fusion.RRFFusion`,负责满足 SPI 生命周期。

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    k:        int   默认 60     RRF 平滑常数
    weights:  dict 默认 {bm25:0.30, vector:0.40, entity:0.15, graph:0.15}
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.internal_models import RankedMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.fuser import Fuser
from memory_app.retrieval.fusion import RRFFusion, parse_rrf_config

logger = logging.getLogger(__name__)


@register
class WeightedRRFFuser(Fuser):
    """加权 RRF 融合插件(检索 默认)。"""

    meta = PluginMeta(
        name="weighted_rrf",
        category="memory.retrieval.fuser",
        version="1.0.0",
        description="加权 Reciprocal Rank Fusion",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "k": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 60},
                "weights": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
            },
        },
    )

    def __init__(self) -> None:
        self._core: RRFFusion = RRFFusion()

    async def start(self, config: Mapping[str, Any]) -> None:
        cfg = parse_rrf_config(dict(config))
        self._core = RRFFusion(config=cfg)
        logger.info("weighted_rrf started: k=%d, weights=%s", cfg.k, cfg.weights)

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"k={self._core.config.k}, "
                f"weights={self._core.config.weights}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    async def fuse(
        self,
        channel_outputs: dict[str, list[RankedMemory]],
        weights: dict[str, float] | None = None,
    ) -> list[RankedMemory]:
        return await self._core.fuse(channel_outputs, weights=weights)

    # ────────────────────────────────────────────────────────────────────────
    # 信号增强:供 SignalBoostStage 调用(非 SPI;鸭子类型)
    # ────────────────────────────────────────────────────────────────────────
    def apply_signal_boost(
        self,
        hits: list[RankedMemory],
        time_decays: dict[str, float] | None = None,
        importances: dict[str, float] | None = None,
    ) -> list[RankedMemory]:
        return self._core.apply_signal_boost(hits, time_decays, importances)


__all__ = ["WeightedRRFFuser"]
