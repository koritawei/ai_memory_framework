"""TaskRunner 工厂。"""

from __future__ import annotations

import logging
from typing import Any

from memory_app.background import BackgroundTaskRunner
from memory_app.settings import Settings
from memory_app.task_queue.arq_runner import ArqTaskRunner

logger = logging.getLogger(__name__)


async def create_task_runner(settings: Settings, clients: Any, dlq: Any) -> Any:
    """按 ``settings.task_runner_backend`` 创建后台任务 runner。

    ``redis`` 后端现使用 **arq**（不再自研 LIST/BRPOPLPUSH）。
    """
    if settings.task_runner_backend == "redis":
        if not settings.redis_url:
            msg = (
                "task_runner_backend=redis but MEMORY_REDIS_URL empty. "
                "Fix MEMORY_REDIS_URL or set task_runner_backend=asyncio."
            )
            if settings.debug:
                logger.warning("%s — fallback to asyncio runner", msg)
                return BackgroundTaskRunner(
                    dlq=dlq,
                    max_concurrent=settings.background_max_concurrent,
                )
            raise RuntimeError(msg)
        runner = ArqTaskRunner(
            redis_url=settings.redis_url,
            queue_name=settings.task_queue_key,
            dlq=dlq,
            max_concurrent=settings.background_max_concurrent,
            in_process=False,
        )
        if settings.task_runner_consumer_enabled:
            await runner.start()
        logger.info(
            "task runner initialized: arq queue=%s consumer=%s",
            settings.task_queue_key,
            settings.task_runner_consumer_enabled,
        )
        return runner
    logger.info(
        "task runner initialized: asyncio max_concurrent=%s",
        settings.background_max_concurrent,
    )
    return BackgroundTaskRunner(
        dlq=dlq,
        max_concurrent=settings.background_max_concurrent,
    )


__all__ = ["create_task_runner"]
