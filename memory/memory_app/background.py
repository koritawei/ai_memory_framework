"""BackgroundTaskRunner —— 异步后台任务调度(冷路径 冷路径)。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
封装 ``asyncio.create_task`` + 重试 + DLQ 三件套,统一冷路径 / 巩固 / 反馈
等"火并忘"任务的入口。离线巩固 切到 Celery / RQ 时只需替换本类实现,业务代码
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
  入注入的 :class:`InMemoryDLQ`(或 离线巩固+ 的持久化 DLQ);**不**抛异常
- 重试期间 logger.warn,DLQ 入队后 logger.error
- 每次重试间隔走指数退避(50ms → 200ms → 800ms;可由 ``RetryPolicy`` 调)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from memory_app.repositories.dlq import DLQRecord

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 重试策略
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class RetryPolicy:
    """指数退避策略。"""

    max_attempts: int = 3
    base_delay_s: float = 0.05
    max_delay_s: float = 5.0
    backoff: float = 4.0  # delay = base * backoff^(attempt-1),封顶 max_delay

    def delay_for(self, attempt: int) -> float:
        """``attempt`` 是第几次重试(从 1 开始)。"""
        d = self.base_delay_s * (self.backoff ** max(0, attempt - 1))
        return min(d, self.max_delay_s)


# ════════════════════════════════════════════════════════════════════════════
# DLQ 协议
# ════════════════════════════════════════════════════════════════════════════
class _DLQLike:
    """DLQ 鸭子类型协议 —— 只要 ``await dlq.enqueue(...)`` 即可。"""

    async def enqueue(self, record: Any) -> None: ...  # pragma: no cover


# ════════════════════════════════════════════════════════════════════════════
# 主类
# ════════════════════════════════════════════════════════════════════════════
class BackgroundTaskRunner:
    """异步任务调度器 —— 冷路径 冷路径默认实现。

    线程安全说明:依赖单事件循环;如需多线程提交,需要外部加锁(本工程的
    asyncio + uvicorn 单进程 / 单 loop 模型已足够)。
    """

    def __init__(
        self,
        dlq: _DLQLike | None = None,
        *,
        retry_policy: RetryPolicy | None = None,
        task_name_prefix: str = "cold_path",
    ) -> None:
        self._dlq = dlq
        self._policy = retry_policy or RetryPolicy()
        self._prefix = task_name_prefix
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._submitted: int = 0
        self._completed: int = 0
        self._failed_to_dlq: int = 0

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
        last_err: BaseException | None = None
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                await coro_factory()
                self._completed += 1
                return
            except asyncio.CancelledError:
                # 关停场景:不重试,不 DLQ
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt >= self._policy.max_attempts:
                    break
                delay = self._policy.delay_for(attempt)
                logger.warning(
                    "background task %s failed (attempt %d/%d, retry in %.2fs): %s",
                    task_id, attempt, self._policy.max_attempts, delay, e,
                )
                await asyncio.sleep(delay)
        # 全部重试失败 → DLQ
        await self._dlq_enqueue(task_id, last_err, on_failure_record)

    async def _dlq_enqueue(
        self, task_id: str | None, err: BaseException | None, base: Optional[dict]
    ) -> None:
        self._failed_to_dlq += 1
        logger.error(
            "background task %s exhausted retries (→ DLQ): %s", task_id, err
        )
        if self._dlq is None:
            return
        record = DLQRecord(
            target=(base or {}).get("target", "background_task"),
            mem_cell_id=task_id or "",
            operation=(base or {}).get("operation", "execute"),
            error=str(err) if err else "",
            retry_count=self._policy.max_attempts,
        )
        try:
            await self._dlq.enqueue(record)
        except Exception as ee:  # noqa: BLE001
            logger.error("DLQ enqueue failed (final): %s", ee)

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
        }


__all__ = ["BackgroundTaskRunner", "RetryPolicy"]
