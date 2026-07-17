"""ArqTaskRunner 接口与 DLQ 语义测试（in_process，无需 Redis）。"""

from __future__ import annotations

import asyncio

import pytest

from memory_app.task_queue.arq_runner import ArqTaskRunner
from memory_app.task_queue.retry import RetryPolicy


@pytest.mark.asyncio
class TestArqTaskRunnerInProcess:
    async def test_submit_and_handle(self):
        runner = ArqTaskRunner(
            queue_name="memory:tasks:test",
            max_concurrent=2,
            in_process=True,
        )
        handled: list[dict] = []

        async def handler(payload: dict) -> None:
            handled.append(payload)

        runner.register_handler("echo", handler)
        await runner.start()
        runner.submit_handler("echo", {"x": 1}, task_id="t1")
        await asyncio.sleep(0.05)
        await runner.shutdown()

        assert handled == [{"x": 1}]
        assert runner.stats()["completed"] == 1
        assert runner.stats()["backend"] == "arq_in_process"

    async def test_unknown_handler_goes_dlq(self):
        class _DLQ:
            def __init__(self) -> None:
                self.records = []

            async def enqueue(self, record) -> None:
                self.records.append(record)

        dlq = _DLQ()
        runner = ArqTaskRunner(
            queue_name="memory:tasks:test",
            in_process=True,
            dlq=dlq,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        await runner.start()
        runner.submit_handler("missing", {}, task_id="m1")
        await asyncio.sleep(0.05)
        await runner.shutdown()
        assert len(dlq.records) == 1
        assert runner.stats()["failed_to_dlq"] == 1

    async def test_dlq_failure_raises_in_execute(self):
        runner = ArqTaskRunner(
            queue_name="memory:tasks:test",
            in_process=True,
            dlq=None,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_s=0.001),
        )

        async def boom(_payload: dict) -> None:
            raise RuntimeError("fail")

        runner.register_handler("boom", boom)
        with pytest.raises(RuntimeError, match="DLQ enqueue failed"):
            await runner._execute(
                "boom", {}, task_id="bad", on_failure_record={"target": "redis_task"}
            )

    async def test_two_in_process_runners_independent(self):
        """in_process 不共享队列；多 worker 竞争由真实 arq/redis 覆盖。"""
        a_handled: list[str] = []
        b_handled: list[str] = []

        async def ha(p: dict) -> None:
            a_handled.append(p["id"])

        async def hb(p: dict) -> None:
            b_handled.append(p["id"])

        a = ArqTaskRunner(queue_name="q", in_process=True, task_name_prefix="a")
        b = ArqTaskRunner(queue_name="q", in_process=True, task_name_prefix="b")
        a.register_handler("work", ha)
        b.register_handler("work", hb)
        await a.start()
        await b.start()
        a.submit_handler("work", {"id": "a1"}, task_id="a1")
        b.submit_handler("work", {"id": "b1"}, task_id="b1")
        await asyncio.sleep(0.05)
        await a.shutdown()
        await b.shutdown()
        assert a_handled == ["a1"]
        assert b_handled == ["b1"]
