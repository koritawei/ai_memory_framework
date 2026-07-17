"""``rule_sbd`` —— Phase 2 规则 SBD 插件(继承 :class:`BoundaryDetector` SPI)。

═══════════════════════════════════════════════════════════════════════════════
职责
═══════════════════════════════════════════════════════════════════════════════
- 满足 :class:`memory_app.plugins.spi.boundary_detector.BoundaryDetector` 契约
- 调用 :func:`memory_app.sbd.should_split` 做单步判定
- 提供批量便利 :meth:`segment` 供 :class:`IngestPipeline.SegmentStage` 使用
  (鸭子类型,不在 SPI 抽象层强制)

═══════════════════════════════════════════════════════════════════════════════
配置 schema
═══════════════════════════════════════════════════════════════════════════════
与 ``noop_sbd`` 兼容(便于 ConfigCenter 灰度切换不改 default.yaml):

::

    time_gap_min:    int [1, 1440]   默认 30
    max_window_size: int [1, 100]    默认 20  (亦接受 max_window_turns 别名)
    max_window_tokens: int [1, ...]  默认 512
    llm_fallback:    bool            默认 false (Phase 3 启用 LLM 兜底时为 true)

Phase 3 ``hybrid_sbd`` 实现会复用本类作为基类,只在 ``llm_fallback=true`` 且
规则置信度低时再调 LLM。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.internal_models import RawData
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.boundary_detector import (
    BoundaryContext,
    BoundaryDetectionResult,
    BoundaryDetector,
)
from memory_app.sbd import SBDConfig, parse_sbd_config, should_split

logger = logging.getLogger(__name__)


@register
class RuleSBD(BoundaryDetector):
    """Phase 2 规则模式 SBD 实现。"""

    meta = PluginMeta(
        name="rule_sbd",
        category="memory.generation.boundary_detector",
        version="1.0.0",
        description="规则 SBD(time_gap + window_turns + window_tokens),Phase 2 默认",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "time_gap_min": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1440,
                    "default": 30,
                },
                "max_window_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "max_window_turns": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
                "max_window_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 512,
                },
                "llm_fallback": {"type": "boolean", "default": False},
            },
        },
    )

    def __init__(self) -> None:
        self._config: SBDConfig = SBDConfig()

    # ════════════════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════════════════
    async def start(self, config: Mapping[str, Any]) -> None:
        self._config = parse_sbd_config(dict(config))
        logger.info(
            "rule_sbd started: time_gap=%s, max_turns=%d, max_tokens=%d",
            self._config.time_gap_threshold,
            self._config.max_window_turns,
            self._config.max_window_tokens,
        )

    async def stop(self) -> None:
        # 无外部资源
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"rule_sbd: time_gap={self._config.time_gap_threshold}, "
                f"max_turns={self._config.max_window_turns}, "
                f"max_tokens={self._config.max_window_tokens}"
            ),
        }

    # ════════════════════════════════════════════════════════════════════════
    # SPI: 单步判定
    # ════════════════════════════════════════════════════════════════════════
    async def detect(
        self,
        history: list[RawData],
        new: list[RawData],
        ctx: BoundaryContext,
    ) -> BoundaryDetectionResult:
        """SPI 契约:判定 ``new`` 是否相对 ``history`` 切边界。

        约定:
        - ``history`` 为空 → ``should_end=False, should_wait=False, reasoning="cold_start"``
        - ``new`` 为空 → 视为 ``should_wait=True``(无新输入,不切也不等)
        - 多条 ``new``:对**第一条** new 做切边界判定(简化模型;批量切分走 :meth:`segment`)
        """
        if not history:
            return BoundaryDetectionResult(
                should_end=False,
                should_wait=False,
                reasoning="cold_start",
                confidence=1.0,
            )
        if not new:
            return BoundaryDetectionResult(
                should_end=False,
                should_wait=False,
                reasoning="empty_new",
                confidence=1.0,
            )
        end, reason = should_split(history, new[0], self._config)
        return BoundaryDetectionResult(
            should_end=end,
            should_wait=False,
            reasoning=reason,
            confidence=1.0,
        )

    # ════════════════════════════════════════════════════════════════════════
    # 便利方法:批量切分
    # ════════════════════════════════════════════════════════════════════════
    async def segment(self, raw_data_list: list[RawData]) -> list[list[RawData]]:
        """把 ``raw_data_list`` 切分为多段 segment。

        本方法**不在 SPI 抽象层**;``IngestPipeline.SegmentStage`` 通过鸭子类型
        调用。其他 SBD 实现(``noop_sbd`` / ``hybrid_sbd``)若想被 IngestPipeline
        使用,需提供同名同语义方法。
        """
        # 直接复用纯函数,行为与 detect 单步判定一致
        from memory_app.sbd import detect_boundaries

        return detect_boundaries(raw_data_list, self._config)
