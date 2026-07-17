"""KVStore SPI —— 键值/文档存储（设计文档 §5.2）。

主存储抽象，承载所有持久化记忆体（MemCell / EpisodicMemory / SemanticMemory）。
默认实现 ``mongo_store``；可换 ``pg_store`` / ``sqlite_store``。
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, AsyncIterator

from memory_app.plugins.base import Plugin


class KVStore(Plugin):
    """键值/文档存储扩展点。

    本接口故意保持「文档型」语义（put 整个 dict），关系型实现需要在内部做
    JSON 列或表-列映射。这是为了让 SPI 既能配 MongoDB 也能配 PostgreSQL。
    """

    @abstractmethod
    async def get(self, collection: str, key: str) -> dict | None:
        """按主键取单条文档。返回 None 表示不存在（不应抛异常）。"""

    @abstractmethod
    async def put(self, collection: str, key: str, value: dict) -> None:
        """upsert 单条文档。

        约定：``value`` 不会被实现修改；实现应做深拷贝再持久化。
        """

    @abstractmethod
    async def delete(self, collection: str, key: str) -> bool:
        """删除单条文档。返回 True 表示真的删了，False 表示原本就不存在。"""

    @abstractmethod
    async def list(
        self, collection: str, filters: dict | None = None, limit: int = 100
    ) -> list[dict]:
        """按 filters 过滤拉取多条文档。

        约定：
        - ``filters`` 应至少支持 ``user_id`` / ``tenant_id`` 精确匹配（多租户隔离）
        - 实现可不支持复杂查询；超出能力时抛 :class:`PluginError(category="config")`
        """

    @abstractmethod
    async def cas(
        self, collection: str, key: str, expected: dict | None, new_value: dict
    ) -> bool:
        """Compare-And-Swap：``expected=None`` 表示仅当不存在时创建。

        约定：返回 True 表示 CAS 成功；返回 False 表示 expected 不匹配（被并发修改）。
        是实现上 Phase 2 写入幂等 + 巩固阶段乐观锁的基础原语。
        """

    @abstractmethod
    async def scan(
        self, collection: str, filters: dict | None = None, batch: int = 200
    ) -> AsyncIterator[dict]:
        """流式扫描，离线巩固任务用。

        约定：必须是 ``async generator`` —— 实现内部按批拉取，调用方按需消费。
        """


__all__ = ["KVStore"]
