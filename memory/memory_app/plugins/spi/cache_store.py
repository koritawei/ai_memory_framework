"""CacheStore SPI —— 通用缓存。

承载 SBD 状态、检索结果缓存、Cross-Encoder LRU 等横切场景。
默认实现 ``redis_store``；测试可换 ``in_memory_cache``。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.plugins.base import Plugin


class CacheStore(Plugin):
    """通用缓存扩展点。"""

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """取字符串值，不存在返回 None。"""

    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """设置值；``ttl_seconds=None`` 表示永久（除非被 evict）。"""

    @abstractmethod
    async def setnx(self, key: str, value: str, ttl_seconds: int) -> bool:
        """SET if Not eXists；返回 True 表示设置成功，False 表示 key 已存在。

        是 :class:`IdempotencyStore` / SBD 状态独占等场景的基础原语。
        """

    @abstractmethod
    async def delete(self, key: str) -> bool: ...

    @abstractmethod
    async def lrange(self, key: str, start: int = 0, end: int = -1) -> list[str]:
        """读取 list 类型范围（SBD 累积消息场景，默认 ``LRANGE 0 -1``）。"""

    @abstractmethod
    async def rpush(self, key: str, *values: str) -> int:
        """list 尾部追加，返回追加后总长度。"""

    @abstractmethod
    async def expire(self, key: str, ttl_seconds: int) -> bool:
        """设置过期时间。"""


__all__ = ["CacheStore"]
