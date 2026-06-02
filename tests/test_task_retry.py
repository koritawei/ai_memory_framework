"""task_queue.retry 共享重试逻辑测试。"""

from __future__ import annotations

import pytest

from memory_app.task_queue.retry import RetryPolicy
from memory_app.task_queue.retry import run_with_retry


class _FakeDLQ:
    def __init__(self) -> None:
        self.records: list = []

    async def enqueue(self, record) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_run_with_retry_succeeds_on_second_attempt():
    calls = {"n": 0}

    async def attempt() -> None:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")

    ok = await run_with_retry(
        attempt,
        policy=RetryPolicy(max_attempts=3, base_delay_s=0.001, backoff=1.0),
        task_id="t1",
        on_failure_record={"target": "test"},
        dlq=None,
    )
    assert ok is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_run_with_retry_enqueues_dlq_after_exhaustion():
    dlq = _FakeDLQ()

    async def attempt() -> None:
        raise RuntimeError("permanent")

    ok = await run_with_retry(
        attempt,
        policy=RetryPolicy(max_attempts=2, base_delay_s=0.001, backoff=1.0),
        task_id="cell-1",
        on_failure_record={"target": "redis_task", "operation": "execute"},
        dlq=dlq,
        default_dlq_target="redis_task",
    )
    assert ok is False
    assert len(dlq.records) == 1
    assert dlq.records[0].mem_cell_id == "cell-1"
