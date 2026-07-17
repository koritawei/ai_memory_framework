"""BM25Store SPI —— BM25 关键词检索后端（设计文档 §5.2 / §6.1）。

默认实现 ``es_store``（Elasticsearch）；可换 ``opensearch_store`` /
``sqlite_fts5_store``。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel, ConfigDict

from memory_app.plugins.base import Plugin


class BM25Hit(BaseModel):
    """BM25 检索单条命中。"""

    model_config = ConfigDict(extra="allow")

    id: str
    score: float          # 原始 BM25 分数（实现自定义量纲）
    payload: dict = {}    # 业务字段


class BM25Store(Plugin):
    """BM25 关键词检索扩展点。"""

    @abstractmethod
    async def index(self, collection: str, doc_id: str, text: str, payload: dict) -> None:
        """单条索引（写入 / 更新）。

        约定：实现应做 lemmatize / 分词；payload 中的字段供检索时返回 / 过滤。
        """

    @abstractmethod
    async def index_batch(
        self, collection: str, docs: list[tuple[str, str, dict]]
    ) -> None:
        """批量索引，``docs = [(doc_id, text, payload), ...]``。"""

    @abstractmethod
    async def search(
        self,
        collection: str,
        query: str,
        k: int,
        filters: dict | None = None,
    ) -> list[BM25Hit]:
        """关键词检索 Top-k。

        约定：
        - 过取（``internal_k = max(k*4, 100)`` 等）由 RetrievalChannel 包装层处理；
          本 SPI 严格按 k 返回
        - 索引中无文档时返回空列表
        """

    @abstractmethod
    async def delete(self, collection: str, doc_id: str) -> bool:
        """单条删除。返回 True/False 同 KVStore 约定。"""

    @abstractmethod
    async def refresh(self, collection: str) -> None:
        """强制 refresh，确保新写入可被搜索（ES 默认 1s 异步 refresh）。"""


__all__ = ["BM25Store", "BM25Hit"]
