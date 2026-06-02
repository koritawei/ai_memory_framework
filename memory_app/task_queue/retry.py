"""任务重试与 DLQ 入队 —— BackgroundTaskRunner / RedisTaskRunner 共享逻辑。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from memory_app.repositories.dlq import DLQRecord

logger = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    """指数退避重试策略。"""

    max_attempts: int = 3
    base_delay_s: float = 0.05
    max_delay_s: float = 5.0
    backoff: float = 4.0

    def delay_for(self, attempt: int) -> float:
        """``attempt`` 是第几次重试(从 1 开始)。"""
        d = self.base_delay_s * (self.backoff ** max(0, attempt - 1))
        return min(d, self.max_delay_s)


async def enqueue_task_dlq(
    dlq: Any | None,
    *,
    task_id: str,
    err: BaseException | None,
    on_failure_record: dict | None,
    policy: RetryPolicy,
    default_target: str = "background_task",
) -> None:
    """重试耗尽后将任务写入 DLQ。"""
    if dlq is None:
        return
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
    except Exception as ee:  # noqa: BLE001
        logger.error("DLQ enqueue failed (final): %s", ee)


async def run_with_retry(
    attempt: Callable[[], Awaitable[None]],
    *,
    policy: RetryPolicy,
    task_id: str,
    on_failure_record: dict | None,
    dlq: Any | None,
    log_name: str = "task",
    default_dlq_target: str = "background_task",
) -> bool:
    """执行 ``attempt``，按策略退避重试；耗尽后入 DLQ。

    :returns: ``True`` 成功；``False`` 已入 DLQ 或 DLQ 不可用
    """
    last_err: BaseException | None = None
    for try_no in range(1, policy.max_attempts + 1):
        try:
            await attempt()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if try_no >= policy.max_attempts:
                break
            delay = policy.delay_for(try_no)
            logger.warning(
                "%s %s failed (attempt %d/%d, retry in %.2fs): %s",
                log_name,
                task_id,
                try_no,
                policy.max_attempts,
                delay,
                e,
            )
            await asyncio.sleep(delay)
    logger.error("%s %s exhausted retries (→ DLQ): %s", log_name, task_id, last_err)
    await enqueue_task_dlq(
        dlq,
        task_id=task_id,
        err=last_err,
        on_failure_record=on_failure_record,
        policy=policy,
        default_target=default_dlq_target,
    )
    return False


__all__ = ["RetryPolicy", "enqueue_task_dlq", "run_with_retry"]
