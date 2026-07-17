"""受控并发工具 + 分布式锁（优先 redis.asyncio.lock）。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def gather_with_limit(
    coros: Iterable[Awaitable[T]],
    limit: int,
    *,
    return_exceptions: bool = False,
) -> list[T | BaseException]:
    """并发执行协程，同时最多 ``limit`` 个在飞。"""
    max_parallel = max(1, int(limit))
    sem = asyncio.Semaphore(max_parallel)

    async def _run(coro: Awaitable[T]) -> T | BaseException:
        async with sem:
            if return_exceptions:
                try:
                    return await coro
                except BaseException as e:  # noqa: BLE001
                    return e
            return await coro

    return await asyncio.gather(
        *(_run(c) for c in coros),
        return_exceptions=return_exceptions,
    )


async def run_with_limit(
    factories: Iterable[Callable[[], Awaitable[Any]]],
    limit: int,
) -> None:
    """对 coroutine factory 列表做有界并发（用于 ingest 回退路径）。"""

    async def _one(factory: Callable[[], Awaitable[Any]]) -> None:
        await factory()

    await gather_with_limit(
        (_one(f) for f in factories),
        limit,
    )


class RedisDistributedLock:
    """Redis 分布式锁。

    优先使用官方 ``redis.asyncio.lock.Lock``（客户端提供 ``.lock()`` 时）；
    测试 FakeRedis 等无该 API 时回退到 SET NX EX。
    """

    def __init__(self, redis_client: Any, key: str, *, ttl_s: int = 60) -> None:
        self._redis = redis_client
        self._key = key
        self._ttl = max(5, int(ttl_s))
        self._oss_lock: Any | None = None
        self._token = uuid.uuid4().hex
        lock_factory = getattr(redis_client, "lock", None)
        if callable(lock_factory):
            try:
                self._oss_lock = lock_factory(
                    name=key,
                    timeout=float(self._ttl),
                    blocking_timeout=0,
                )
            except TypeError:
                # 部分 mock 签名不同
                try:
                    self._oss_lock = lock_factory(key, timeout=float(self._ttl))
                except Exception as e:  # noqa: BLE001
                    logger.debug("redis.lock() unavailable, fallback SET NX: %s", e)
                    self._oss_lock = None
            except Exception as e:  # noqa: BLE001
                logger.debug("redis.lock() unavailable, fallback SET NX: %s", e)
                self._oss_lock = None

    async def acquire(self) -> bool:
        if self._oss_lock is not None:
            try:
                return bool(await self._oss_lock.acquire(blocking=False))
            except Exception as e:  # noqa: BLE001
                logger.warning("redis.lock acquire failed for %s: %s", self._key, e)
                return False
        try:
            ok = await self._redis.set(self._key, self._token, nx=True, ex=self._ttl)
            return bool(ok)
        except Exception as e:  # noqa: BLE001
            logger.warning("redis lock acquire failed for %s: %s", self._key, e)
            return False

    async def release(self) -> None:
        if self._oss_lock is not None:
            try:
                await self._oss_lock.release()
            except Exception as e:  # noqa: BLE001
                logger.warning("redis.lock release failed for %s: %s", self._key, e)
            return
        try:
            current = await self._redis.get(self._key)
            if current == self._token:
                await self._redis.delete(self._key)
        except Exception as e:  # noqa: BLE001
            logger.warning("redis lock release failed for %s: %s", self._key, e)


__all__ = [
    "gather_with_limit",
    "run_with_limit",
    "RedisDistributedLock",
]
