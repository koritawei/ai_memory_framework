"""``noop_sbd`` —— 脚手架/1 占位 SBD（继承 :class:`BoundaryDetector` SPI）。

当前版本起本插件已切到正式 SPI 抽象，方法签名与未来真实 ``rule_sbd`` /
``hybrid_sbd``（写入热路径 落地）兼容。继续承担"打通通路 + 默认安全行为"职责：
:meth:`detect` 永远返回 ``should_wait=True`` —— 不切边界，让上游继续累积。
"""

from __future__ import annotations

from typing import Any, Mapping

from memory_app.internal_models import RawData
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.boundary_detector import (
    BoundaryContext,
    BoundaryDetectionResult,
    BoundaryDetector,
)


@register
class NoopSBD(BoundaryDetector):
    """脚手架/1 stub —— 永不切边界。"""

    meta = PluginMeta(
        name="noop_sbd",
        category="memory.generation.boundary_detector",
        version="0.1.0",
        description="脚手架/1 stub —— 永不切边界",
        # JSON Schema：与未来 hybrid_sbd 的 schema 子集兼容，
        # 便于配置切换后无需修改 default.yaml 中的参数块
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "time_gap_min": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 30},
                "max_window_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                "llm_fallback": {"type": "boolean", "default": False},
            },
        },
    )

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}

    async def start(self, config: Mapping[str, Any]) -> None:
        # 仅记录配置便于 health/metrics 输出；不连接任何外部依赖
        self._config = dict(config)

    async def stop(self) -> None:
        self._config = {}

    async def detect(
        self,
        history: list[RawData],
        new: list[RawData],
        ctx: BoundaryContext,
    ) -> BoundaryDetectionResult:
        """脚手架/1 stub：永远要求 caller 等待更多消息。

        遵守 SPI 约定：history 为空时返回 ``should_end=False, should_wait=False,
        reasoning="cold_start"``，让 SBD 调用方据此判定是否首条消息。
        """
        if not history:
            return BoundaryDetectionResult(
                should_end=False,
                should_wait=False,
                reasoning="cold_start",
                confidence=1.0,
            )
        return BoundaryDetectionResult(
            should_end=False,
            should_wait=True,
            reasoning="noop_sbd_stub",
            confidence=1.0,
        )

    async def health(self) -> dict:
        return {"status": "ok", "detail": "noop_sbd ready"}
