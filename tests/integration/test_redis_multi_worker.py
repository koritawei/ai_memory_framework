"""arq 多 worker 集成测试。

默认用 in_process 验证接口；若环境有 Redis（MEMORY_TEST_REDIS_URL）则跑真实 arq 竞争消费。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from memory_app.task_queue.arq_runner import ArqTaskRunner

pytestmark = pytest.mark.integration

QUEUE_KEY = "memory:tasks:multi_worker_arq"


async def _wait_until(predicate, *, timeout_s: float = 8.0, interval_s: float = 0.05) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval_s)
    raise AssertionError("condition not met before timeout")


@pytest.mark.asyncio
async def test_in_process_handlers_no_duplicates_locally():
    """单进程内串行提交不应重复。"""
    redis_url = os.environ.get("MEMORY_TEST_REDIS_URL")
    if redis_url:
        pytest.skip("real redis path covered by test_arq_two_workers_share_queue")

    handled: list[str] = []

    async def handler(payload: dict) -> None:
        handled.append(payload["task_id"])

    runner = ArqTaskRunner(queue_name=QUEUE_KEY, in_process=True, max_concurrent=4)
    runner.register_handler("work", handler)
    await runner.start()
    for i in range(12):
        runner.submit_handler("work", {"task_id": f"t{i}"}, task_id=f"t{i}")
    await _wait_until(lambda: len(handled) >= 12)
    await runner.shutdown()
    assert len(handled) == 12
    assert set(handled) == {f"t{i}" for i in range(12)}


@pytest.mark.asyncio
async def test_arq_two_workers_share_queue():
    redis_url = os.environ.get("MEMORY_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("set MEMORY_TEST_REDIS_URL to run real arq multi-worker test")

    handled: list[str] = []
    lock = asyncio.Lock()
    seen: set[str] = set()
    duplicates: list[str] = []

    async def handler(payload: dict) -> None:
        tid = payload["task_id"]
        async with lock:
            if tid in seen:
                duplicates.append(tid)
            seen.add(tid)
            handled.append(tid)
        await asyncio.sleep(0.01)

    producer = ArqTaskRunner(
        redis_url=redis_url,
        queue_name=QUEUE_KEY,
        max_concurrent=4,
        task_name_prefix="prod",
    )
    worker_a = ArqTaskRunner(
        redis_url=redis_url,
        queue_name=QUEUE_KEY,
        max_concurrent=4,
        task_name_prefix="wa",
    )
    worker_b = ArqTaskRunner(
        redis_url=redis_url,
        queue_name=QUEUE_KEY,
        max_concurrent=4,
        task_name_prefix="wb",
    )
    worker_a.register_handler("work", handler)
    worker_b.register_handler("work", handler)

    await worker_a.start()
    await worker_b.start()

    n = 20
    for i in range(n):
        producer.submit_handler("work", {"task_id": f"t{i}"}, task_id=f"arq-{i}")
    await _wait_until(lambda: len(handled) >= n, timeout_s=15)

    await producer.shutdown()
    await worker_a.shutdown()
    await worker_b.shutdown()

    assert duplicates == []
    assert len(handled) == n
