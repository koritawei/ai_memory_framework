"""RedisTaskRunner 可靠投递测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from memory_app.task_queue.redis_runner import RedisTaskRunner
from tests.fixtures.fake_redis import FakeRedisLists


@pytest.mark.asyncio
class TestRedisTaskRunnerReliableDelivery:
    async def test_brpoplpush_then_ack_clears_processing(self):
        redis = FakeRedisLists()
        runner = RedisTaskRunner(redis, "memory:tasks:test", max_concurrent=2)
        handled: list[dict] = []

        async def handler(payload: dict) -> None:
            handled.append(payload)

        runner.register_handler("echo", handler)
        runner.submit_handler("echo", {"x": 1}, task_id="t1")
        await asyncio.sleep(0.05)
        assert len(redis.lists.get("memory:tasks:test", [])) == 1

        await runner.start()
        await asyncio.sleep(0.1)
        await runner.shutdown()

        assert handled == [{"x": 1}]
        assert redis.lists.get("memory:tasks:test:processing", []) == []

    async def test_recovery_requeues_processing_on_start(self):
        redis = FakeRedisLists()
        stale = json.dumps({"handler": "echo", "payload": {"y": 2}, "task_id": "t2", "on_failure_record": {}})
        redis.lists["memory:tasks:test:processing"] = [stale]

        runner = RedisTaskRunner(redis, "memory:tasks:test", max_concurrent=2)
        handled: list[dict] = []

        async def handler(payload: dict) -> None:
            handled.append(payload)

        runner.register_handler("echo", handler)
        await runner.start()
        await asyncio.sleep(0.1)
        await runner.shutdown()

        assert handled == [{"y": 2}]
        assert redis.lists.get("memory:tasks:test:processing", []) == []

    async def test_cancelled_handler_leaves_message_in_processing(self):
        redis = FakeRedisLists()
        runner = RedisTaskRunner(redis, "memory:tasks:test", max_concurrent=1)

        async def slow_handler(_payload: dict) -> None:
            await asyncio.sleep(10)

        runner.register_handler("slow", slow_handler)
        raw = json.dumps({"handler": "slow", "payload": {}, "task_id": "s", "on_failure_record": {}})
        redis.lists["memory:tasks:test:processing"] = [raw]

        task = asyncio.create_task(runner._run_message_guarded(json.loads(raw), raw))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert raw in redis.lists.get("memory:tasks:test:processing", [])
