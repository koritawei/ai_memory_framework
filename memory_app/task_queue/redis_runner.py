"""Redis 分布式任务队列 —— 可靠投递（pending → processing → ACK）。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from memory_app.task_queue.retry import RetryPolicy
from memory_app.task_queue.retry import run_with_retry

logger = logging.getLogger(__name__)

TaskHandler = Callable[[dict[str, Any]], Awaitable[Any]]

_PROCESSING_SUFFIX = ":processing"


class RedisTaskRunner:
    """将可序列化任务写入 Redis LIST；BRPOPLPUSH 到 processing 队列后 ACK。"""

    def __init__(
        self,
        redis_client: Any,
        queue_key: str,
        *,
        dlq: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        task_name_prefix: str = "redis_task",
        brpop_timeout_s: float = 2.0,
        max_concurrent: int | None = None,
    ) -> None:
        self._redis = redis_client
        self._queue_key = queue_key
        self._processing_key = f"{queue_key}{_PROCESSING_SUFFIX}"
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
        self._in_flight = 0
        self._max_concurrent = max(1, int(max_concurrent)) if max_concurrent else 8
        self._handler_sem = asyncio.Semaphore(self._max_concurrent)
        self._inflight_handlers: set[asyncio.Task[Any]] = set()

    def register_handler(self, name: str, handler: TaskHandler) -> None:
        self._handlers[name] = handler

    async def start(self) -> None:
        if self._consumer_task is not None:
            return
        await self._recover_processing_queue()
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

    submit = submit_handler

    async def _enqueue(self, message: dict[str, Any]) -> None:
        raw = json.dumps(message, ensure_ascii=False)
        await self._redis.rpush(self._queue_key, raw)
        self._submitted += 1

    async def _recover_processing_queue(self) -> None:
        """崩溃恢复：processing 队列中未 ACK 的消息重回 pending。"""
        recovered = 0
        while True:
            raw = await self._redis.rpop(self._processing_key)
            if not raw:
                break
            await self._redis.lpush(self._queue_key, raw)
            recovered += 1
        if recovered:
            logger.warning(
                "redis task runner recovered %d unacked message(s) from %s",
                recovered,
                self._processing_key,
            )

    async def _consume_loop(self) -> None:
        while not self._closed:
            try:
                raw = await self._redis.brpoplpush(
                    self._queue_key,
                    self._processing_key,
                    timeout=int(max(1, self._brpop_timeout)),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("redis task brpoplpush failed: %s", e)
                await asyncio.sleep(0.5)
                continue
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("invalid redis task payload: %s", raw[:200])
                await self._ack(raw)
                continue
            task = asyncio.create_task(
                self._run_message_guarded(message, raw),
                name=f"{self._prefix}:handler",
            )
            self._inflight_handlers.add(task)
            task.add_done_callback(self._inflight_handlers.discard)

    async def _ack(self, raw: str) -> None:
        """从 processing 队列移除已处理消息。"""
        try:
            await self._redis.lrem(self._processing_key, 1, raw)
        except Exception as e:  # noqa: BLE001
            logger.error("redis task ack failed: %s", e)

    async def _run_message_guarded(self, message: dict[str, Any], raw: str) -> None:
        async with self._handler_sem:
            self._in_flight += 1
            try:
                await self._run_message(message)
                await self._ack(raw)
            except asyncio.CancelledError:
                raise
            finally:
                self._in_flight -= 1

    async def _run_message(self, message: dict[str, Any]) -> None:
        handler_name = message.get("handler", "")
        handler = self._handlers.get(handler_name)
        task_id = message.get("task_id") or handler_name
        payload = message.get("payload") or {}
        base = message.get("on_failure_record") or {}
        if handler is None:
            from memory_app.task_queue.retry import enqueue_task_dlq

            self._failed_to_dlq += 1
            await enqueue_task_dlq(
                self._dlq,
                task_id=task_id,
                err=RuntimeError(f"unknown handler: {handler_name}"),
                on_failure_record=base,
                policy=self._policy,
                default_target="redis_task",
            )
            return

        async def attempt() -> None:
            await handler(payload)

        if await run_with_retry(
            attempt,
            policy=self._policy,
            task_id=task_id,
            on_failure_record=base,
            dlq=self._dlq,
            log_name="redis task",
            default_dlq_target="redis_task",
        ):
            self._completed += 1
        else:
            self._failed_to_dlq += 1

    async def shutdown(self, *, timeout_s: float = 5.0) -> None:
        self._closed = True
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await asyncio.wait_for(self._consumer_task, timeout=timeout_s)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._consumer_task = None
        if self._inflight_handlers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._inflight_handlers, return_exceptions=True),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                for t in list(self._inflight_handlers):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*self._inflight_handlers, return_exceptions=True)

    def stats(self) -> dict:
        return {
            "submitted": self._submitted,
            "completed": self._completed,
            "failed_to_dlq": self._failed_to_dlq,
            "in_flight": self._in_flight,
            "closed": self._closed,
            "backend": "redis",
            "queue_key": self._queue_key,
            "processing_key": self._processing_key,
            "max_concurrent": self._max_concurrent,
        }


__all__ = ["RedisTaskRunner"]
