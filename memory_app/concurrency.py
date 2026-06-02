"""受控并发工具 —— 限制 asyncio.gather fan-out。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

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


__all__ = ["gather_with_limit", "run_with_limit"]
