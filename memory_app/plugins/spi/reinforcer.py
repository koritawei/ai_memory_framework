"""Reinforcer SPI —— 反馈强化（设计文档 §7.5 / §6.2.1）。

把显式 / 隐式反馈映射为 strength 增量，并触发 Tier Promotion（Langevin 位置回拉）。
默认实现 ``synaptic_plasticity_reinforcer``。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.plugins.base import Plugin
from memory_app.schemas.feedback import FeedbackType
from .forgetting_policy import MemoryRef


class Reinforcer(Plugin):
    """反馈强化扩展点。"""

    @abstractmethod
    async def reinforce(
        self,
        memory: MemoryRef,
        feedback_type: FeedbackType,
        signal_value: float = 0.0,
    ) -> float:
        """根据反馈更新 strength，返回新值。

        约定（设计文档 §7.5 反馈信号映射）：

        | feedback_type      | 默认 signal_value | 效果 |
        | ------------------ | ----------------- | ---- |
        | EXPLICIT_CONFIRM   | +1.0              | 强强化 |
        | POSITIVE           | +0.3              | 弱强化 |
        | NEGATIVE           | -0.5              | 弱衰减 |
        | CORRECTION         | -2.0              | 强衰减 |
        | DELETION_REQUEST   | -10.0             | 触发主动删除 |

        - ``signal_value=0.0`` 表示按 feedback_type 取默认值
        - 公式：``S_new = clip(S_old + η × signal - λ × Δt, 0, S_max)``，
          η=0.3、λ=0.01/天、S_max=5.0
        - 实现还应同步触发 Tier Promotion（Langevin 位置向球心回拉）
        """


__all__ = ["Reinforcer"]
