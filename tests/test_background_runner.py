"""BackgroundTaskRunner 并发上限测试。"""

from __future__ import annotations

import asyncio

import pytest

from memory_app.background import BackgroundTaskRunner


@pytest.mark.asyncio
async def test_max_concurrent_limits_parallel_execution():
    runner = BackgroundTaskRunner(max_concurrent=2)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _slow():
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1

    tasks = [
        runner.submit(lambda i=i: _slow(), task_id=str(i)) for i in range(6)
    ]
    await asyncio.gather(*tasks)
    assert peak <= 2
