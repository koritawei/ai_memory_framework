"""Redis 分布式任务队列 —— 多 worker 通过 BRPOP 竞争消费。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from memory_app.background import BackgroundTaskRunner, RetryPolicy
from memory_app.repositories.dlq import DLQRecord

logger = logging.getLogger(__name__)

TaskHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class RedisTaskRunner:
    """将可序列化任务写入 Redis LIST；本进程内后台 loop BRPOP 消费。

    与 Celery/RQ 同类语义，但复用已有 redis.asyncio 客户端，无需额外 worker 进程。
    多 uvicorn worker 共享同一 queue key，每条任务仅被一个消费者处理。
    """

    def __init__(
        self,
        redis_client: Any,
        queue_key: str,
        *,
        dlq: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        task_name_prefix: str = "redis_task",
        brpop_timeout_s: float = 2.0,
    ) -> None:
        self._redis = redis_client
        self._queue_key = queue_key
        self._dlq = dlq
        self._policy = retry_policy or RetryPolicy()
        self._prefix = task_name_prefix
        self._brpop_timeout = brpop_timeout_s
        self._handlers: dict[str, TaskHandler] = {}
        self._consumer_task: asyncio.Task[Any] | None = None
        self._closed = False
        self._submitted = 0
        self._completed = 0
        self._failed_to_dlq = 0

    def register_handler(self, name: str, handler: TaskHandler) -> None:
        self._handlers[name] = handler

    async def start(self) -> None:
        if self._consumer_task is not None:
            return
        self._consumer_task = asyncio.create_task(
            self._consume_loop(), name=f"{self._prefix}:consumer"
        )

    def submit_handler(
        self,
        handler_name: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        on_failure_record: Optional[dict] = None,
    ) -> None:
        """入队命名 handler + JSON payload（跨进程可序列化）。"""
        if self._closed:
            raise RuntimeError("RedisTaskRunner is closed")
        message = {
            "handler": handler_name,
            "payload": payload,
            "task_id": task_id or "",
            "on_failure_record": on_failure_record or {},
        }
        asyncio.create_task(self._enqueue(message))

    submit = submit_handler  # 与 BackgroundTaskRunner 别名对齐

    async def _enqueue(self, message: dict[str, Any]) -> None:
        await self._redis.rpush(
            self._queue_key,
            json.dumps(message, ensure_ascii=False),
        )
        self._submitted += 1

    async def _consume_loop(self) -> None:
        while not self._closed:
            try:
                item = await self._redis.brpop(
                    self._queue_key, timeout=int(max(1, self._brpop_timeout))
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("redis task brpop failed: %s", e)
                await asyncio.sleep(0.5)
                continue
            if not item:
                continue
            _, raw = item
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("invalid redis task payload: %s", raw[:200])
                continue
            await self._run_message(message)

    async def _run_message(self, message: dict[str, Any]) -> None:
        handler_name = message.get("handler", "")
        handler = self._handlers.get(handler_name)
        task_id = message.get("task_id") or handler_name
        payload = message.get("payload") or {}
        base = message.get("on_failure_record") or {}
        if handler is None:
            await self._dlq_enqueue(
                task_id, RuntimeError(f"unknown handler: {handler_name}"), base
            )
            return
        last_err: BaseException | None = None
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                await handler(payload)
                self._completed += 1
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt >= self._policy.max_attempts:
                    break
                await asyncio.sleep(self._policy.delay_for(attempt))
        await self._dlq_enqueue(task_id, last_err, base)

    async def _dlq_enqueue(
        self, task_id: str, err: BaseException | None, base: dict
    ) -> None:
        self._failed_to_dlq += 1
        logger.error("redis task %s exhausted retries (→ DLQ): %s", task_id, err)
        if self._dlq is None:
            return
        record = DLQRecord(
            target=base.get("target", "redis_task"),
            mem_cell_id=task_id or "",
            operation=base.get("operation", "execute"),
            error=str(err) if err else "",
            retry_count=self._policy.max_attempts,
        )
        try:
            await self._dlq.enqueue(record)
        except Exception as ee:  # noqa: BLE001
            logger.error("DLQ enqueue failed (final): %s", ee)

    async def shutdown(self, *, timeout_s: float = 5.0) -> None:
        self._closed = True
        if self._consumer_task is None:
            return
        self._consumer_task.cancel()
        try:
            await asyncio.wait_for(self._consumer_task, timeout=timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        self._consumer_task = None

    def stats(self) -> dict:
        return {
            "submitted": self._submitted,
            "completed": self._completed,
            "failed_to_dlq": self._failed_to_dlq,
            "in_flight": 0,
            "closed": self._closed,
            "backend": "redis",
            "queue_key": self._queue_key,
        }


__all__ = ["RedisTaskRunner"]
