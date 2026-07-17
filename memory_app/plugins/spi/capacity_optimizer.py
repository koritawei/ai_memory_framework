"""CapacityOptimizer SPI —— 容量约束优化（设计文档 §7.7）。

默认实现 ``greedy_capacity_optimizer``：贪心 + 引用检查 + 安全边际（每轮 ≤ 10%）。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.plugins.base import Plugin
from .forgetting_policy import MemoryRef


class CapacityOptimizer(Plugin):
    """容量优化扩展点。"""

    @abstractmethod
    async def select_to_forget(
        self,
        memories: list[MemoryRef],
        capacity: int,
    ) -> list[str]:
        """从 memories 中选出"应被遗忘"的 memory_id 列表。

        目标函数（设计文档 §7.7）::

            F = arg min Σ s_i  s.t.  |M - F| ≤ capacity

        约定：
        - 优先级：P0 危险内容（SRC ≤ -10）强制遗忘；P1 ARCHIVED → COLD → WARM
        - 引用检查：被存活 SemanticMemory 的 ``source_episodes`` 引用的
          EpisodicMemory **不**进入物理删除候选（应改归档降级）
        - 安全边际：单轮最多删除 ``excess × 10%``，剩余留待下次（避免一次性删崩）
        - 实现是纯函数，不应直接调存储；交给上游执行删除
        """


__all__ = ["CapacityOptimizer"]
