"""ForgettingPolicy SPI —— 遗忘策略。

核心实现 默认 ``ebbinghaus_v1``（艾宾浩斯简单衰减）；
写入热路径+ 可切到 ``langevin_sde``（Poincaré 球面 + Langevin SDE）。
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from memory_app.internal_models import MemoryState
from memory_app.plugins.base import Plugin


class MemoryRef(BaseModel):
    """遗忘策略仅需的"记忆引用"轻量视图。

    避免在 SPI 中传整条 EpisodicMemory / SemanticMemory（含大量与遗忘
    无关的字段，且二者结构不同）。
    """

    model_config = ConfigDict(extra="allow")

    memory_id: str
    memory_type: str  # EPISODIC / SEMANTIC / PROFILE
    state: MemoryState = MemoryState.ACTIVE
    strength: float = 1.0
    access_count: int = 0
    importance_score: float = 0.0
    created_at: datetime
    last_recalled_at: datetime | None = None
    #: 写入热路径+ Langevin SDE 启用时填充
    langevin_position: list[float] = []


class ForgettingPolicy(Plugin):
    """遗忘策略扩展点。"""

    @abstractmethod
    async def retention_score(self, memory: MemoryRef, now: datetime) -> float:
        """计算保留度 ``[0, 1]``。

        约定：
        - 返回值越大 = 越应该保留；< ``threshold_forget``（默认 0.15）进入
          遗忘候选池
        - 实现应是纯函数（无 IO），便于离线批量计算
        """

    @abstractmethod
    async def step(self, memories: list[MemoryRef], dt_seconds: float) -> list[MemoryRef]:
        """批量演化一步（如 Langevin SDE 单步），更新内部状态。

        约定：
        - ``dt_seconds`` 是上次 step 至今的时间间隔，由调度器传入
        - 返回**新的** MemoryRef 列表（不修改入参）
        - 实现应保证幂等可重入：同 ``dt_seconds`` 重复执行不会"过度演化"
        """


__all__ = ["ForgettingPolicy", "MemoryRef"]
