"""任务重试与 DLQ 入队 —— 退避由 tenacity 承担，DLQ/ACK 语义保留自研。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from memory_app.repositories.dlq import DLQRecord

logger = logging.getLogger(__name__)


class TaskOutcome(str, Enum):
    """任务终态 —— 决定 Redis processing 是否可 ACK。"""

    SUCCESS = "success"
    DLQ_OK = "dlq_ok"
    DLQ_FAILED = "dlq_failed"

    @property
    def ackable(self) -> bool:
        return self in (TaskOutcome.SUCCESS, TaskOutcome.DLQ_OK)


@dataclass
class RetryPolicy:
    """指数退避重试策略（映射到 tenacity wait/stop）。"""

    max_attempts: int = 3
    base_delay_s: float = 0.05
    max_delay_s: float = 5.0
    backoff: float = 4.0

    def delay_for(self, attempt: int) -> float:
        """``attempt`` 是第几次重试(从 1 开始)。保留给测试/诊断。"""
        d = self.base_delay_s * (self.backoff ** max(0, attempt - 1))
        return min(d, self.max_delay_s)


def _should_retry(exc: BaseException) -> bool:
    return not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))


async def enqueue_task_dlq(
    dlq: Any | None,
    *,
    task_id: str,
    err: BaseException | None,
    on_failure_record: dict | None,
    policy: RetryPolicy,
    default_target: str = "background_task",
) -> bool:
    """重试耗尽后将任务写入 DLQ。

    :returns: ``True`` 已确认入队；``False`` 无 DLQ 或入队失败（调用方不得 ACK）。
    """
    if dlq is None:
        logger.error(
            "DLQ unavailable; cannot park failed task %s (will not ACK)", task_id
        )
        return False
    base = on_failure_record or {}
    record = DLQRecord(
        target=base.get("target", default_target),
        mem_cell_id=task_id or "",
        operation=base.get("operation", "execute"),
        error=str(err) if err else "",
        retry_count=policy.max_attempts,
    )
    try:
        await dlq.enqueue(record)
        return True
    except Exception as ee:  # noqa: BLE001
        logger.error("DLQ enqueue failed (final): %s", ee)
        return False


async def run_with_retry(
    attempt: Callable[[], Awaitable[None]],
    *,
    policy: RetryPolicy,
    task_id: str,
    on_failure_record: dict | None,
    dlq: Any | None,
    log_name: str = "task",
    default_dlq_target: str = "background_task",
) -> TaskOutcome:
    """执行 ``attempt``，经 tenacity 指数退避重试；耗尽后入 DLQ。

    :returns: :class:`TaskOutcome`（成功 / DLQ 已确认 / DLQ 失败）
    """
    last_err: BaseException | None = None
    try:
        async for attempt_state in AsyncRetrying(
            stop=stop_after_attempt(max(1, policy.max_attempts)),
            wait=wait_exponential(
                multiplier=max(policy.base_delay_s, 0.001),
                exp_base=max(policy.backoff, 1.0),
                max=policy.max_delay_s,
            ),
            retry=retry_if_exception(_should_retry),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                "%s %s failed (attempt %s/%s, retry in %.2fs): %s",
                log_name,
                task_id,
                rs.attempt_number,
                policy.max_attempts,
                rs.next_action.sleep if rs.next_action else 0.0,  # type: ignore[union-attr]
                rs.outcome.exception() if rs.outcome else None,  # type: ignore[union-attr]
            ),
        ):
            with attempt_state:
                await attempt()
        return TaskOutcome.SUCCESS
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        last_err = e

    logger.error("%s %s exhausted retries (→ DLQ): %s", log_name, task_id, last_err)
    parked = await enqueue_task_dlq(
        dlq,
        task_id=task_id,
        err=last_err,
        on_failure_record=on_failure_record,
        policy=policy,
        default_target=default_dlq_target,
    )
    return TaskOutcome.DLQ_OK if parked else TaskOutcome.DLQ_FAILED


__all__ = [
    "RetryPolicy",
    "TaskOutcome",
    "enqueue_task_dlq",
    "run_with_retry",
]
