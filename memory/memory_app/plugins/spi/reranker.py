"""Reranker SPI —— 重排。

链式调用：MMR (λ=0.7) → Cross-Encoder（Top-20，可选）。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.internal_models import RankedMemory
from memory_app.plugins.base import Plugin


class Reranker(Plugin):
    """重排扩展点。

    可被链式串联使用：``MMRReranker`` 先做多样性去重，
    ``CrossEncoderReranker`` 再做精细相关性评分。
    """

    @abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: list[RankedMemory],
        top_k: int | None = None,
    ) -> list[RankedMemory]:
        """对候选集重排。

        约定：
        - 返回**新列表**，不修改入参
        - ``top_k=None`` 时返回与 candidates 等长（仅重排）；
          ``top_k`` 给定时截断至 Top-k
        - 重排后 ``RankedMemory.score`` 应反映本次 rerank 的得分（可保留原 score
          至 metadata）
        - 实现内部异常应包装为 :class:`PluginError`
        """


__all__ = ["Reranker"]
