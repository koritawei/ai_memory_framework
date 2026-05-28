"""VectorStore SPI —— 向量索引。

默认实现 ``milvus_store``；可换 ``qdrant_store`` / ``pgvector_store``。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel, ConfigDict

from memory_app.plugins.base import Plugin


class VectorItem(BaseModel):
    """upsert 单条向量。"""

    model_config = ConfigDict(extra="allow")

    id: str
    vector: list[float]
    payload: dict = {}  # 业务字段（tenant_id / user_id / memory_type 等）


class VectorHit(BaseModel):
    """检索单条命中。"""

    model_config = ConfigDict(extra="allow")

    id: str
    score: float
    payload: dict = {}


class VectorStore(Plugin):
    """向量索引扩展点。"""

    @abstractmethod
    async def upsert(self, collection: str, items: list[VectorItem]) -> None:
        """批量 upsert。

        约定：
        - ``items`` 内全部向量维度必须一致；不一致应抛 :class:`PluginError(category="config")`
        - upsert 是幂等操作 —— 相同 id 重复提交结果一致
        """

    @abstractmethod
    async def search(
        self,
        collection: str,
        query_vec: list[float],
        k: int,
        filters: dict | None = None,
    ) -> list[VectorHit]:
        """ANN 检索 Top-k。

        约定：
        - ``filters`` 应支持 ``tenant_id`` / ``user_id`` 等精确匹配（多租户隔离）
        - 返回列表按 ``score`` 降序
        - 索引未建立时应返回空列表（不应抛异常）
        """

    @abstractmethod
    async def delete(self, collection: str, ids: list[str]) -> int:
        """批量删除，返回实际删除数。"""

    @abstractmethod
    async def flush(self, collection: str) -> None:
        """强制刷盘，确保新写入立即可被检索。"""


__all__ = ["VectorStore", "VectorItem", "VectorHit"]
