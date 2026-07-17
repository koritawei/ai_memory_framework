"""单路召回通道(设计文档 §6.1)。

- :class:`BaseRetrievalChannel`  模板方法,统一依赖检查 / 解析 / 排序
- :class:`BM25Channel`           ES BM25 关键词召回
- :class:`VectorChannel`         Milvus 向量语义召回
"""

from memory_app.retrieval.channels.base import BaseRetrievalChannel
from memory_app.retrieval.channels.bm25 import BM25Channel
from memory_app.retrieval.channels.vector import VectorChannel

__all__ = ["BaseRetrievalChannel", "BM25Channel", "VectorChannel"]
