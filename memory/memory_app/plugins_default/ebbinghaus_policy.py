"""``ebbinghaus_v1`` —— 反馈与生命周期 默认 ForgettingPolicy。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.forgetting_policy.ForgettingPolicy` 的默认实现。
内部用 :func:`memory_app.scoring.ebbinghaus_retention` 算保留度。

写入热路径+ 启用 Langevin SDE 时切到 ``langevin_sde`` 插件,业务代码零改动。

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    s_base:           float 默认 4.0   (天,strength=1 时半衰期)
    threshold_forget: float 默认 0.15  (低于此值进入遗忘候选池)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.forgetting_policy import ForgettingPolicy, MemoryRef
from memory_app.scoring import (
    EbbinghausConfig,
    ebbinghaus_retention,
    parse_ebbinghaus_config,
)

logger = logging.getLogger(__name__)


@register
class EbbinghausPolicy(ForgettingPolicy):
    """艾宾浩斯简单衰减(反馈与生命周期 默认)。"""

    meta = PluginMeta(
        name="ebbinghaus_v1",
        category="memory.lifecycle.forgetting_policy",
        version="1.0.0",
        description="艾宾浩斯遗忘曲线 + access/state 三因子修正",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "s_base": {
                    "type": "number", "minimum": 0.1, "maximum": 365.0, "default": 4.0
                },
                "threshold_forget": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.15
                },
            },
        },
    )

    def __init__(self) -> None:
        self._config: EbbinghausConfig = EbbinghausConfig()

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._config = parse_ebbinghaus_config(dict(config))
        logger.info(
            "ebbinghaus_v1 started: s_base=%.2f, threshold_forget=%.2f",
            self._config.s_base, self._config.threshold_forget,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"s_base={self._config.s_base}d, "
                f"threshold_forget={self._config.threshold_forget}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    async def retention_score(self, memory: MemoryRef, now: datetime) -> float:
        age_days = max(
            0.0,
            (_normalize(now) - _normalize(memory.created_at)).total_seconds() / 86400.0,
        )
        return ebbinghaus_retention(
            age_days=age_days,
            strength=float(memory.strength),
            access_count=int(memory.access_count),
            config=self._config,
        )

    async def step(
        self, memories: list[MemoryRef], dt_seconds: float
    ) -> list[MemoryRef]:
        """核心实现 简化:不做位置演化,仅按 dt_seconds 在 ``last_recalled_at``
        基础上做一步衰减(等价 retention_score 的离线版本,不修改 strength)。

        约定:返回**新列表**(不改入参)。
        """
        out: list[MemoryRef] = []
        now = datetime.now(timezone.utc)
        for m in memories:
            new = m.model_copy(deep=True)
            # ebbinghaus_v1 不改 strength,仅供 retention_score 评估;此处直接复制
            out.append(new)
        return out


def _normalize(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


__all__ = ["EbbinghausPolicy"]
