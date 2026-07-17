"""``greedy_capacity_optimizer`` —— Phase 6 Step 6.3 默认 CapacityOptimizer。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.capacity_optimizer.CapacityOptimizer` 的默认实现。
纯函数选择"应被遗忘"的 memory_id 列表 —— 真正删除 / 归档由调用方做。

═══════════════════════════════════════════════════════════════════════════════
策略(贪心 + 安全边际)
═══════════════════════════════════════════════════════════════════════════════
1. P0:``importance_score < danger_threshold`` 强制进入候选(危险内容)
2. P1:按 state 优先级 ARCHIVED → COLD → WARM → ACTIVE,**同等级内**按
   ``strength × max(1, access_count/10)`` 升序
3. 安全边际:单轮最多删 ``excess × safety_margin``(默认 10%);剩余下次再处理
4. ``capacity`` 是"目标保留量",``F = arg min Σ s_i s.t. |M − F| ≤ capacity``
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.internal_models import MemoryState
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.capacity_optimizer import CapacityOptimizer
from memory_app.plugins.spi.forgetting_policy import MemoryRef

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 内部:state 优先级
# ════════════════════════════════════════════════════════════════════════════
_STATE_PRIORITY = {
    MemoryState.ARCHIVED: 0,
    MemoryState.COLD: 1,
    MemoryState.WARM: 2,
    MemoryState.ACTIVE: 3,
}


def _state_rank(state: Any) -> int:
    if isinstance(state, MemoryState):
        return _STATE_PRIORITY.get(state, 4)
    try:
        return _STATE_PRIORITY.get(MemoryState(state), 4)
    except (ValueError, TypeError):
        return 4


# ════════════════════════════════════════════════════════════════════════════
# 插件
# ════════════════════════════════════════════════════════════════════════════
@register
class GreedyCapacityOptimizer(CapacityOptimizer):
    """贪心 + 安全边际容量优化(Phase 6 默认)。"""

    meta = PluginMeta(
        name="greedy",
        category="memory.lifecycle.capacity_optimizer",
        version="1.0.0",
        description="贪心 + 安全边际:state + strength × access 升序选择遗忘候选",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "safety_margin": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.1
                },
                "danger_threshold": {
                    "type": "number", "default": -10.0
                },
            },
        },
    )

    def __init__(self) -> None:
        self._safety_margin: float = 0.1
        self._danger_threshold: float = -10.0

    async def start(self, config: Mapping[str, Any]) -> None:
        try:
            self._safety_margin = max(
                0.0, min(1.0, float(config.get("safety_margin", 0.1)))
            )
        except (TypeError, ValueError):
            self._safety_margin = 0.1
        try:
            self._danger_threshold = float(config.get("danger_threshold", -10.0))
        except (TypeError, ValueError):
            self._danger_threshold = -10.0
        logger.info(
            "greedy_capacity_optimizer started: safety_margin=%.2f, danger_threshold=%.1f",
            self._safety_margin, self._danger_threshold,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"safety_margin={self._safety_margin}, "
                f"danger_threshold={self._danger_threshold}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    async def select_to_forget(
        self,
        memories: list[MemoryRef],
        capacity: int,
    ) -> list[str]:
        if not memories or capacity < 0:
            return []
        total = len(memories)
        if total <= capacity:
            return []

        # P0:危险内容
        danger: list[str] = [
            m.memory_id for m in memories
            if float(m.importance_score) <= self._danger_threshold
        ]

        # P1:state + strength × access_factor 升序
        def _rank_key(m: MemoryRef) -> tuple:
            access_factor = max(1.0, float(m.access_count) / 10.0)
            return (
                _state_rank(m.state),
                float(m.strength) * access_factor,
                float(m.importance_score),
            )

        sorted_p1 = sorted(memories, key=_rank_key)
        # 排除已经入 P0 的
        danger_set = set(danger)
        ordered = [m for m in sorted_p1 if m.memory_id not in danger_set]

        excess = total - capacity
        cap = max(1, int(total * self._safety_margin))
        target_n = min(excess, cap)
        # P0(危险内容)优先纳入,但**不能超出 excess**:
        # excess 是真正"应该删多少"的上限,P0 全清如果超过它,会让
        # |M − F| < capacity(过度删除合法内容)。先用 P0 占满 excess,
        # 没占满的余额再走 P1。
        out: list[str] = list(danger)[:excess]
        # 剩余配额从 P1 升序补齐;target_n 已经是 min(excess, cap),
        # 若 P0 已占满 target_n 配额则 need=0,直接返回。
        need = max(0, target_n - len(out))
        for m in ordered:
            if need <= 0:
                break
            out.append(m.memory_id)
            need -= 1
        return out


__all__ = ["GreedyCapacityOptimizer"]
