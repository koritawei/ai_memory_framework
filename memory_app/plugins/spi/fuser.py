"""Fuser SPI —— 多路融合。

把多个 :class:`RetrievalChannel` 的输出融合为统一排名。
默认实现 ``weighted_rrf``：``RRFScore = Σ w_ch / (k + rank_ch)``，``k=60``。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.internal_models import RankedMemory
from memory_app.plugins.base import Plugin


class Fuser(Plugin):
    """多路融合扩展点。"""

    @abstractmethod
    async def fuse(
        self,
        channel_outputs: dict[str, list[RankedMemory]],
        weights: dict[str, float] | None = None,
    ) -> list[RankedMemory]:
        """把多通道结果融合为单一排名列表。

        约定：
        - ``channel_outputs`` 的 key = 通道名，value = 该通道 Top-k 列表
        - ``weights`` 缺失时用各通道默认权重（实现内部决定）
        - **rank-level 融合**（如 RRF）天然处理跨通道分数尺度不一致 —— 默认实现
          不做归一化；线性加权类实现需自带归一化
        - 融合后的 ``RankedMemory.score`` 应表达"综合相关性"
        """


__all__ = ["Fuser"]
