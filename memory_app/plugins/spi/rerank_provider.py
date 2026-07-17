"""RerankProvider SPI —— Cross-Encoder 重排 Provider（设计文档 §13.2）。

默认实现 ``deepinfra_qwen3_reranker``（Qwen3-Reranker-4B）；
可换 ``cohere_reranker`` / ``local_ms_marco``。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel

from memory_app.plugins.base import Plugin


class RerankResult(BaseModel):
    """rerank 单条结果。"""

    index: int       # 在原 docs 列表中的索引
    score: float     # 相关性得分 [0, 1]


class RerankProvider(Plugin):
    """Cross-Encoder Rerank Provider 扩展点。"""

    @abstractmethod
    async def rerank(self, query: str, docs: list[str]) -> list[RerankResult]:
        """对 (query, doc) pair 评分。

        约定：
        - 返回**新列表**，按 ``score`` 降序
        - 返回长度等于 ``len(docs)`` —— 不丢任何文档（截断由调用方决定）
        - 每个 ``RerankResult.index`` 对应原 ``docs`` 中的位置
        - 实现可对 ``docs`` 做批拆分（如 numpy.array_split 10 份并发）
        """


__all__ = ["RerankProvider", "RerankResult"]
