"""``synaptic_plasticity_reinforcer`` —— 反馈与生命周期 默认 Reinforcer。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.reinforcer.Reinforcer` 的默认实现。

核心算法委托给 :func:`memory_app.scoring.compute_strength_delta`
(``S_new = clip(S_old + η × signal − λ × Δt_days, 0, S_max)``),本类负责:

- 把 SPI :class:`MemoryRef` 转成强化所需的最小字段集
- 解析 ``signal_value=0`` 的默认信号值表
- ``reinforce`` 仅返回**新 strength**,不做持久化(持久化由
  :class:`FeedbackService` 在调用方做,与 SPI 契约一致)

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    eta:              float 默认 0.3
    lambda_per_day:   float 默认 0.01
    s_max:            float 默认 5.0
    default_signals:  dict  覆盖默认信号表(可选)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.forgetting_policy import MemoryRef
from memory_app.plugins.spi.reinforcer import Reinforcer
from memory_app.schemas.feedback import FeedbackType
from memory_app.scoring import (
    ReinforceConfig,
    compute_strength_delta,
    parse_reinforce_config,
    resolve_signal,
)

logger = logging.getLogger(__name__)


@register
class SynapticPlasticityReinforcer(Reinforcer):
    """突触可塑性反馈强化(反馈与生命周期 默认)。"""

    meta = PluginMeta(
        name="synaptic_plasticity_reinforcer",
        category="memory.lifecycle.reinforcer",
        version="1.0.0",
        description="突触可塑性强化:S_new = clip(S_old + η×signal − λ×Δt, 0, S_max)",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "eta": {"type": "number", "minimum": 0.0, "maximum": 5.0, "default": 0.3},
                "lambda_per_day": {"type": "number", "minimum": 0.0, "default": 0.01},
                "s_max": {"type": "number", "minimum": 0.1, "default": 5.0},
                "default_signals": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
            },
        },
    )

    def __init__(self) -> None:
        self._config: ReinforceConfig = ReinforceConfig()
        self._reinforce_calls: int = 0

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._config = parse_reinforce_config(dict(config))
        logger.info(
            "synaptic_plasticity_reinforcer started: eta=%.3f, lambda=%.4f/day, s_max=%.2f",
            self._config.eta, self._config.lambda_per_day, self._config.s_max,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"eta={self._config.eta}, lambda={self._config.lambda_per_day}, "
                f"s_max={self._config.s_max}, calls={self._reinforce_calls}"
            ),
        }

    async def metrics(self) -> dict:
        return {"synaptic_reinforce_calls": self._reinforce_calls}

    # ────────────────────────────────────────────────────────────────────────
    # SPI:reinforce
    # ────────────────────────────────────────────────────────────────────────
    async def reinforce(
        self,
        memory: MemoryRef,
        feedback_type: FeedbackType,
        signal_value: float = 0.0,
    ) -> float:
        """计算并返回**新 strength**;不做持久化。"""
        self._reinforce_calls += 1
        signal = resolve_signal(feedback_type, signal_value, self._config)
        new_strength, delta = compute_strength_delta(
            old_strength=float(memory.strength),
            signal=signal,
            last_at=memory.last_recalled_at,
            now=datetime.now(timezone.utc),
            config=self._config,
        )
        logger.debug(
            "reinforce: %s type=%s signal=%.2f Δ=%.3f → strength=%.3f",
            memory.memory_id, feedback_type.value, signal, delta, new_strength,
        )
        return new_strength

    # ────────────────────────────────────────────────────────────────────────
    # 工具(非 SPI;FeedbackService 直接调用便于一次得到 delta + signal)
    # ────────────────────────────────────────────────────────────────────────
    def explain(
        self,
        memory: MemoryRef,
        feedback_type: FeedbackType,
        signal_value: float = 0.0,
    ) -> dict[str, Any]:
        signal = resolve_signal(feedback_type, signal_value, self._config)
        new_strength, delta = compute_strength_delta(
            old_strength=float(memory.strength),
            signal=signal,
            last_at=memory.last_recalled_at,
            now=datetime.now(timezone.utc),
            config=self._config,
        )
        return {
            "old_strength": float(memory.strength),
            "new_strength": new_strength,
            "delta": delta,
            "signal": signal,
            "feedback_type": feedback_type.value,
        }


__all__ = ["SynapticPlasticityReinforcer"]
