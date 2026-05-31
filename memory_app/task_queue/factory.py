"""TaskRunner 工厂。"""

from __future__ import annotations

import logging
from typing import Any

from memory_app.background import BackgroundTaskRunner
from memory_app.settings import Settings
from memory_app.task_queue.redis_runner import RedisTaskRunner

logger = logging.getLogger(__name__)


async def create_task_runner(settings: Settings, clients: Any, dlq: Any) -> Any:
    """按 ``settings.task_runner_backend`` 创建后台任务 runner。"""
    if settings.task_runner_backend == "redis":
        if clients.redis_client is None:
            logger.warning(
                "task_runner_backend=redis but redis unavailable; fallback asyncio"
            )
            return BackgroundTaskRunner(dlq=dlq)
        runner = RedisTaskRunner(
            clients.redis_client,
            settings.task_queue_key,
            dlq=dlq,
        )
        if settings.task_runner_consumer_enabled:
            await runner.start()
        logger.info(
            "task runner initialized: redis queue=%s consumer=%s",
            settings.task_queue_key,
            settings.task_runner_consumer_enabled,
        )
        return runner
    logger.info("task runner initialized: asyncio")
    return BackgroundTaskRunner(dlq=dlq)


__all__ = ["create_task_runner"]
