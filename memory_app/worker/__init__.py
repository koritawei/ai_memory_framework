"""独立 Redis 冷路径 Worker（类 RQ 消费进程）。"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from memory_app.deps import app_state
from memory_app.settings import get_settings
from memory_app.task_queue.redis_runner import RedisTaskRunner

logger = logging.getLogger(__name__)


async def _run() -> int:
    settings = get_settings()
    if settings.task_runner_backend != "redis":
        logger.error(
            "task_runner_backend=%s; memory-worker requires redis",
            settings.task_runner_backend,
        )
        return 1

    await app_state.init(settings)
    runner = app_state.background_runner
    if not isinstance(runner, RedisTaskRunner):
        logger.error("cold path / redis runner not configured")
        await app_state.close()
        return 1

    if runner._consumer_task is None:
        await runner.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows
            signal.signal(sig, lambda *_: stop.set())

    logger.info(
        "memory-worker running (queue=%s, dlq=%s)",
        settings.task_queue_key,
        settings.dlq_backend,
    )
    await stop.wait()
    logger.info("memory-worker shutting down")
    await app_state.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``memory-worker`` 或 ``python -m memory_app.worker``。"""
    _ = argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
