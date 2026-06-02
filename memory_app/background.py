"""BackgroundTaskRunner —— 异步后台任务调度(Phase 3 冷路径)。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
封装 ``asyncio.create_task`` + 重试 + DLQ 三件套,统一冷路径 / 巩固 / 反馈
等"火并忘"任务的入口。Phase 6 切到 Celery / RQ 时只需替换本类实现,业务代码
(``ColdPathService``)零改动。

═══════════════════════════════════════════════════════════════════════════════
设计
═══════════════════════════════════════════════════════════════════════════════
- :meth:`submit`             火并忘提交一个 coro,失败按 ``retry_policy`` 自动退避
- :meth:`submit_and_track`   提交并返回 :class:`asyncio.Task`(测试 / 评测用得上)
- :meth:`shutdown`           lifespan 关闭时取消未完成任务 + 等待已开始的优雅完成

═══════════════════════════════════════════════════════════════════════════════
重试与 DLQ
═══════════════════════════════════════════════════════════════════════════════
- 重试次数耗尽后,把任务包装为 :class:`memory_app.repositories.dlq.DLQRecord`
  入注入的 :class:`InMemoryDLQ`(或 Phase 6+ 的持久化 DLQ);**不**抛异常
- 重试期间 logger.warn,DLQ 入队后 logger.error
- 每次重试间隔走指数退避(50ms → 200ms → 800ms;可由 ``RetryPolicy`` 调)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from memory_app.repositories.dlq import DLQRecord
from memory_app.task_queue.retry import RetryPolicy

logger = logging.getLogger(__name__)


# RetryPolicy 已迁至 task_queue.retry；此处 re-export 保持向后兼容。
class _DLQLike:
    """DLQ 鸭子类型协议 —— 只要 ``await dlq.enqueue(...)`` 即可。"""

    async def enqueue(self, record: Any) -> None: ...  # pragma: no cover


# ════════════════════════════════════════════════════════════════════════════
# 主类
# ════════════════════════════════════════════════════════════════════════════
class BackgroundTaskRunner:
    """异步任务调度器 —— Phase 3 冷路径默认实现。

    线程安全说明:依赖单事件循环;如需多线程提交,需要外部加锁(本工程的
    asyncio + uvicorn 单进程 / 单 loop 模型已足够)。
    """

    def __init__(
        self,
        dlq: _DLQLike | None = None,
        *,
        retry_policy: RetryPolicy | None = None,
        task_name_prefix: str = "cold_path",
        max_concurrent: int | None = None,
    ) -> None:
        self._dlq = dlq
        self._policy = retry_policy or RetryPolicy()
        self._prefix = task_name_prefix
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._submitted: int = 0
        self._completed: int = 0
        self._failed_to_dlq: int = 0
        self._max_concurrent = (
            int(max_concurrent) if max_concurrent and max_concurrent > 0 else None
        )
        self._semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(self._max_concurrent) if self._max_concurrent else None
        )

    # ────────────────────────────────────────────────────────────────────────
    # 提交
    # ────────────────────────────────────────────────────────────────────────
    def submit(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        *,
        task_id: str | None = None,
        on_failure_record: Optional[dict] = None,
    ) -> asyncio.Task[Any]:
        """火并忘提交。

        :param coro_factory: 无参 callable,返回**新**的 coroutine —— 重试时会被多次调用
        :param task_id:      日志 / DLQ 标识(常用 ``mem_cell_id`` / ``episode_id``)
        :param on_failure_record:
            可选基础 dict,DLQ 入队时将合并 ``{target=..., error=..., retry_count=...}``。
        """
        if self._closed:
            raise RuntimeError("BackgroundTaskRunner is closed")
        task = asyncio.create_task(
            self._run_with_retry(coro_factory, task_id, on_failure_record),
            name=f"{self._prefix}:{task_id or '?'}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._submitted += 1
        return task

    submit_and_track = submit  # 别名

    # ────────────────────────────────────────────────────────────────────────
    # 运行
    # ────────────────────────────────────────────────────────────────────────
    async def _run_with_retry(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        task_id: str | None,
        on_failure_record: Optional[dict],
    ) -> None:
        from memory_app.task_queue.retry import run_with_retry

        sem = self._semaphore

        async def attempt() -> None:
            if sem is not None:
                await sem.acquire()
            try:
                await coro_factory()
            finally:
                if sem is not None:
                    sem.release()

        if await run_with_retry(
            attempt,
            policy=self._policy,
            task_id=task_id or "?",
            on_failure_record=on_failure_record,
            dlq=self._dlq,
            log_name="background task",
            default_dlq_target="background_task",
        ):
            self._completed += 1
        else:
            self._failed_to_dlq += 1

    async def _dlq_enqueue(
        self, task_id: str | None, err: BaseException | None, base: Optional[dict]
    ) -> None:
        """Deprecated: use task_queue.retry.enqueue_task_dlq."""
        from memory_app.task_queue.retry import enqueue_task_dlq

        self._failed_to_dlq += 1
        await enqueue_task_dlq(
            self._dlq,
            task_id=task_id or "",
            err=err,
            on_failure_record=base,
            policy=self._policy,
            default_target="background_task",
        )

    # ────────────────────────────────────────────────────────────────────────
    # 关停
    # ────────────────────────────────────────────────────────────────────────
    async def shutdown(self, *, timeout_s: float = 5.0) -> None:
        """优雅关停:等待已提交的任务完成,超时后取消。"""
        self._closed = True
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            for t in list(self._tasks):
                if not t.done():
                    t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ────────────────────────────────────────────────────────────────────────
    # 监控
    # ────────────────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        return {
            "submitted": self._submitted,
            "completed": self._completed,
            "failed_to_dlq": self._failed_to_dlq,
            "in_flight": len(self._tasks),
            "closed": self._closed,
            "max_concurrent": self._max_concurrent,
        }


__all__ = ["BackgroundTaskRunner", "RetryPolicy"]
