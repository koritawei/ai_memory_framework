"""Redis 多 worker 集成测试 —— 共享队列竞争消费 + processing 恢复。

使用 ``FakeRedisLists`` 模拟 Redis，无需外部 Redis 实例。
打 ``@pytest.mark.integration``；显式 ``pytest -m integration`` 运行。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from memory_app.task_queue.redis_runner import RedisTaskRunner
from tests.fixtures.fake_redis import FakeRedisLists

pytestmark = pytest.mark.integration

QUEUE_KEY = "memory:tasks:multi_worker"


async def _wait_until(predicate, *, timeout_s: float = 5.0, interval_s: float = 0.05) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval_s)
    raise AssertionError("condition not met before timeout")


@pytest.mark.asyncio
class TestRedisMultiWorker:
    async def test_two_workers_consume_all_tasks_without_duplicates(self):
        redis = FakeRedisLists()
        handled: list[str] = []
        duplicates: list[str] = []
        seen: set[str] = set()
        handle_lock = asyncio.Lock()

        async def handler(payload: dict) -> None:
            task_id = payload["task_id"]
            async with handle_lock:
                if task_id in seen:
                    duplicates.append(task_id)
                seen.add(task_id)
                handled.append(task_id)
            await asyncio.sleep(0.002)

        worker_a = RedisTaskRunner(
            redis, QUEUE_KEY, max_concurrent=4, task_name_prefix="worker_a", brpop_timeout_s=0.1
        )
        worker_b = RedisTaskRunner(
            redis, QUEUE_KEY, max_concurrent=4, task_name_prefix="worker_b", brpop_timeout_s=0.1
        )
        worker_a.register_handler("work", handler)
        worker_b.register_handler("work", handler)

        n = 24
        for i in range(n):
            worker_a.submit_handler("work", {"task_id": f"t{i}"}, task_id=f"t{i}")

        await worker_a.start()
        await worker_b.start()

        await _wait_until(lambda: len(handled) >= n)

        await worker_a.shutdown()
        await worker_b.shutdown()

        assert duplicates == []
        assert len(handled) == n
        assert set(handled) == {f"t{i}" for i in range(n)}
        assert redis.lists.get(f"{QUEUE_KEY}:processing", []) == []
        assert redis.lists.get(QUEUE_KEY, []) == []

    async def test_recovery_after_worker_stops_with_unacked_message(self):
        redis = FakeRedisLists()
        handled: list[str] = []

        async def handler(payload: dict) -> None:
            handled.append(payload["task_id"])

        worker_b = RedisTaskRunner(
            redis, QUEUE_KEY, max_concurrent=1, task_name_prefix="worker_b", brpop_timeout_s=0.1
        )
        worker_b.register_handler("work", handler)

        stale = json.dumps(
            {
                "handler": "work",
                "payload": {"task_id": "recover-me"},
                "task_id": "recover-me",
                "on_failure_record": {},
            }
        )
        redis.lists[f"{QUEUE_KEY}:processing"] = [stale]

        await worker_b.start()
        await _wait_until(lambda: "recover-me" in handled)

        await worker_b.shutdown()

        assert handled == ["recover-me"]
        assert redis.lists.get(f"{QUEUE_KEY}:processing", []) == []
