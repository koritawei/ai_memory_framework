"""基于 arq 的分布式任务队列 —— 替代自研 Redis LIST / BRPOPLPUSH。

接口与历史 ``RedisTaskRunner`` 对齐：
``register_handler`` / ``submit_handler`` / ``start`` / ``shutdown`` / ``stats``。

- 生产：``arq`` 负责任务持久化、多 worker 竞争、崩溃恢复
- 单测：``in_process=True`` 时走进程内调度，无需 Redis
- 重试与 DLQ：仍经 ``task_queue.retry``（tenacity + TaskOutcome）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from memory_app.task_queue.retry import RetryPolicy, TaskOutcome, enqueue_task_dlq, run_with_retry

logger = logging.getLogger(__name__)

TaskHandler = Callable[[dict[str, Any]], Awaitable[Any]]

_DISPATCH_NAME = "memory_dispatch"


def _redis_settings_from_url(redis_url: str, *, conn_timeout: int = 5):
    from arq.connections import RedisSettings

    u = urlparse(redis_url)
    return RedisSettings(
        host=u.hostname or "localhost",
        port=u.port or 6379,
        database=int((u.path or "/0").lstrip("/") or 0),
        password=u.password,
        username=u.username,
        conn_timeout=conn_timeout,
    )


class ArqTaskRunner:
    """arq 任务 runner（跨进程可序列化 handler 名 + JSON payload）。"""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        queue_name: str = "memory:tasks",
        dlq: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        task_name_prefix: str = "arq_task",
        max_concurrent: int | None = None,
        in_process: bool = False,
        redis_pool: Any | None = None,
        # 兼容旧 RedisTaskRunner(redis_client, queue_key, ...) 位置参数风格的关键字
        redis_client: Any | None = None,
        **_ignored: Any,
    ) -> None:
        # 旧调用：RedisTaskRunner(redis_client, queue_key) —— 无 URL 时强制 in_process
        if redis_client is not None and redis_url is None and redis_pool is None:
            in_process = True
        self._redis_url = redis_url
        self._queue_name = queue_name
        self._dlq = dlq
        self._policy = retry_policy or RetryPolicy()
        self._prefix = task_name_prefix
        self._max_concurrent = max(1, int(max_concurrent)) if max_concurrent else 8
        self._in_process = bool(in_process)
        self._external_pool = redis_pool

        self._handlers: dict[str, TaskHandler] = {}
        self._pool: Any | None = None
        self._worker: Any | None = None
        self._worker_task: asyncio.Task[Any] | None = None
        self._started = False
        self._closed = False
        self._submitted = 0
        self._completed = 0
        self._failed_to_dlq = 0
        self._in_flight = 0
        self._inflight_local: set[asyncio.Task[Any]] = set()

    @property
    def worker_id(self) -> str:
        return f"arq:{self._prefix}"

    @property
    def processing_key(self) -> str:
        return f"{self._queue_name}:arq"

    def register_handler(self, name: str, handler: TaskHandler) -> None:
        self._handlers[name] = handler

    async def start(self) -> None:
        """启动 arq Worker（消费端）。仅入队的 API 进程不要调用。"""
        if self._started:
            return
        self._started = True
        if self._in_process:
            logger.info("arq task runner in-process mode (no redis worker)")
            return

        from arq import create_pool
        from arq.worker import Worker, func

        if self._external_pool is not None:
            self._pool = self._external_pool
        else:
            if not self._redis_url:
                raise RuntimeError("ArqTaskRunner requires redis_url when not in_process")
            self._pool = await create_pool(
                _redis_settings_from_url(self._redis_url),
                default_queue_name=self._queue_name,
            )

        runner = self

        async def memory_dispatch(
            ctx: dict[str, Any],
            handler_name: str,
            payload: dict[str, Any],
            task_id: str = "",
            on_failure_record: dict[str, Any] | None = None,
        ) -> None:
            await runner._execute(
                handler_name,
                payload or {},
                task_id=task_id or handler_name,
                on_failure_record=on_failure_record or {},
            )

        self._worker = Worker(
            functions=[func(memory_dispatch, name=_DISPATCH_NAME, max_tries=1)],
            redis_pool=self._pool,
            queue_name=self._queue_name,
            max_jobs=self._max_concurrent,
            max_tries=1,
            handle_signals=False,
            job_completion_wait=5,
            ctx={"runner": self},
            keep_result=0,
            poll_delay=0.2,
        )
        self._worker_task = asyncio.create_task(
            self._worker.async_run(), name=f"{self._prefix}:arq-worker"
        )
        logger.info(
            "arq task runner started queue=%s max_jobs=%s",
            self._queue_name,
            self._max_concurrent,
        )

    def submit_handler(
        self,
        handler_name: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        on_failure_record: Optional[dict] = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("ArqTaskRunner is closed")
        tid = task_id or ""
        record = on_failure_record or {}
        if self._in_process:
            task = asyncio.create_task(
                self._execute(
                    handler_name,
                    payload,
                    task_id=tid or handler_name,
                    on_failure_record=record,
                ),
                name=f"{self._prefix}:{tid or handler_name}",
            )
            self._inflight_local.add(task)
            task.add_done_callback(self._inflight_local.discard)
            self._submitted += 1
            return
        asyncio.create_task(
            self._enqueue(handler_name, payload, tid, record),
            name=f"{self._prefix}:enqueue",
        )

    submit = submit_handler

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        if self._external_pool is not None:
            self._pool = self._external_pool
            return self._pool
        from arq import create_pool

        if not self._redis_url:
            raise RuntimeError("ArqTaskRunner redis_url missing")
        self._pool = await create_pool(
            _redis_settings_from_url(self._redis_url),
            default_queue_name=self._queue_name,
        )
        return self._pool

    async def _enqueue(
        self,
        handler_name: str,
        payload: dict[str, Any],
        task_id: str,
        on_failure_record: dict[str, Any],
    ) -> None:
        pool = await self._ensure_pool()
        try:
            await pool.enqueue_job(
                _DISPATCH_NAME,
                handler_name,
                payload,
                task_id,
                on_failure_record,
                _queue_name=self._queue_name,
                _job_id=task_id or None,
            )
            self._submitted += 1
        except Exception as e:  # noqa: BLE001
            logger.error("arq enqueue failed handler=%s: %s", handler_name, e)
            raise

    async def _execute(
        self,
        handler_name: str,
        payload: dict[str, Any],
        *,
        task_id: str,
        on_failure_record: dict[str, Any],
    ) -> None:
        self._in_flight += 1
        try:
            handler = self._handlers.get(handler_name)
            if handler is None:
                parked = await enqueue_task_dlq(
                    self._dlq,
                    task_id=task_id,
                    err=RuntimeError(f"unknown handler: {handler_name}"),
                    on_failure_record=on_failure_record,
                    policy=self._policy,
                    default_target="redis_task",
                )
                self._failed_to_dlq += 1
                if not parked:
                    raise RuntimeError(f"unknown handler and DLQ failed: {handler_name}")
                return

            async def attempt() -> None:
                await handler(payload)

            outcome = await run_with_retry(
                attempt,
                policy=self._policy,
                task_id=task_id,
                on_failure_record=on_failure_record,
                dlq=self._dlq,
                log_name="arq task",
                default_dlq_target="redis_task",
            )
            if outcome is TaskOutcome.SUCCESS:
                self._completed += 1
            else:
                self._failed_to_dlq += 1
            if outcome is TaskOutcome.DLQ_FAILED:
                raise RuntimeError(f"DLQ enqueue failed for task {task_id}")
        finally:
            self._in_flight -= 1

    async def shutdown(self, *, timeout_s: float = 5.0) -> None:
        self._closed = True
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._worker.close(), timeout=timeout_s)
            except Exception as e:  # noqa: BLE001
                logger.debug("arq worker close: %s", e)
            self._worker = None
            # Worker.close 会关闭 pool
            self._pool = None
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await asyncio.wait_for(self._worker_task, timeout=timeout_s)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._worker_task = None
        if self._inflight_local:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._inflight_local, return_exceptions=True),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                for t in list(self._inflight_local):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*self._inflight_local, return_exceptions=True)
        if self._pool is not None and self._external_pool is None:
            try:
                close = getattr(self._pool, "aclose", None) or getattr(self._pool, "close", None)
                if close is not None:
                    await close()
            except Exception as e:  # noqa: BLE001
                logger.debug("arq pool close: %s", e)
            self._pool = None
        self._started = False

    def stats(self) -> dict:
        return {
            "submitted": self._submitted,
            "completed": self._completed,
            "failed_to_dlq": self._failed_to_dlq,
            "in_flight": self._in_flight,
            "closed": self._closed,
            "backend": "arq_in_process" if self._in_process else "arq",
            "queue_key": self._queue_name,
            "processing_key": self.processing_key,
            "worker_id": self.worker_id,
            "max_concurrent": self._max_concurrent,
        }


# 向后兼容
RedisTaskRunner = ArqTaskRunner

__all__ = ["ArqTaskRunner", "RedisTaskRunner"]
