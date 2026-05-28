"""IdempotencyStore SPI —— 写入幂等键。

把"幂等"语义独立出 :class:`CacheStore`，便于：
- 业务层调用更直观（``claim`` vs ``setnx``）
- 实现可选不同后端（Redis / Mongo unique index / ZooKeeper）
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel

from memory_app.plugins.base import Plugin


class IdempotencyClaim(BaseModel):
    """``claim`` 调用结果。"""

    claimed: bool                 # True = 第一次写入；False = 已被其他请求占用
    existing_value: dict | None = None  # claimed=False 时为已有值（如 mem_cell_id）


class IdempotencyStore(Plugin):
    """写入幂等扩展点。"""

    @abstractmethod
    async def claim(
        self, key: str, value: dict, ttl_seconds: int = 86400
    ) -> IdempotencyClaim:
        """原子尝试占用 key。

        约定：
        - 同一 key 多次 claim：第一次返回 ``claimed=True``，后续返回
          ``claimed=False`` 且 ``existing_value`` 为第一次的 value
        - ``ttl_seconds`` 默认 24h ——  推荐值
        - 实现必须保证原子性（Redis SETNX / Mongo unique index）
        """

    @abstractmethod
    async def release(self, key: str) -> bool:
        """主动释放 key（用于错误回滚场景）。"""


__all__ = ["IdempotencyStore", "IdempotencyClaim"]
