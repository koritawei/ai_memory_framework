"""写入幂等键实现 —— InMemory / Redis（对接 IdempotencyStore SPI 语义）。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from memory_app.plugins.spi.idempotency_store import IdempotencyClaim

logger = logging.getLogger(__name__)


class InMemoryIdempotencyStore:
    """进程内幂等存储（单测 / 单副本开发）。"""

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict, float]] = {}  # key -> (value, expire_at)

    def _purge(self) -> None:
        now = time.time()
        expired = [k for k, (_v, exp) in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]

    async def claim(
        self, key: str, value: dict, ttl_seconds: int = 86400
    ) -> IdempotencyClaim:
        self._purge()
        if key in self._store:
            existing, _exp = self._store[key]
            return IdempotencyClaim(claimed=False, existing_value=dict(existing))
        self._store[key] = (dict(value), time.time() + max(1, int(ttl_seconds)))
        return IdempotencyClaim(claimed=True, existing_value=None)

    async def complete(self, key: str, value: dict, ttl_seconds: int = 86400) -> None:
        """写入最终结果（持有 claim 后调用）。"""
        self._store[key] = (dict(value), time.time() + max(1, int(ttl_seconds)))

    async def release(self, key: str) -> bool:
        return self._store.pop(key, None) is not None


class RedisIdempotencyStore:
    """Redis SET NX 幂等存储。"""

    def __init__(self, redis_client: Any, *, key_prefix: str = "memory:idem:") -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def claim(
        self, key: str, value: dict, ttl_seconds: int = 86400
    ) -> IdempotencyClaim:
        full = self._full_key(key)
        raw = json.dumps(value, ensure_ascii=False)
        ok = await self._redis.set(full, raw, nx=True, ex=max(1, int(ttl_seconds)))
        if ok:
            return IdempotencyClaim(claimed=True, existing_value=None)
        existing_raw = await self._redis.get(full)
        existing: dict | None = None
        if existing_raw:
            if isinstance(existing_raw, bytes):
                existing_raw = existing_raw.decode("utf-8")
            try:
                existing = json.loads(existing_raw)
            except json.JSONDecodeError:
                existing = {"raw": existing_raw}
        return IdempotencyClaim(claimed=False, existing_value=existing)

    async def complete(self, key: str, value: dict, ttl_seconds: int = 86400) -> None:
        full = self._full_key(key)
        raw = json.dumps(value, ensure_ascii=False)
        await self._redis.set(full, raw, ex=max(1, int(ttl_seconds)))

    async def release(self, key: str) -> bool:
        full = self._full_key(key)
        n = await self._redis.delete(full)
        return bool(n)


def create_idempotency_store(settings: Any, clients: Any) -> Any | None:
    """按运行时依赖创建幂等存储；无 Redis 时回退内存（仅单副本语义）。"""
    redis = getattr(clients, "redis_client", None)
    if redis is not None:
        return RedisIdempotencyStore(redis)
    logger.info("idempotency store: in-memory (no redis client)")
    return InMemoryIdempotencyStore()


__all__ = [
    "InMemoryIdempotencyStore",
    "RedisIdempotencyStore",
    "create_idempotency_store",
]
