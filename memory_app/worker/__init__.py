"""独立 arq 冷路径 Worker。"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from memory_app.deps import app_state
from memory_app.settings import get_settings
from memory_app.task_queue.arq_runner import ArqTaskRunner

logger = logging.getLogger(__name__)


async def _run() -> int:
    settings = get_settings()
    if settings.task_runner_backend != "redis":
        logger.error(
            "task_runner_backend=%s; memory-worker requires redis/arq",
            settings.task_runner_backend,
        )
        return 1

    # worker 进程必须消费队列
    if hasattr(settings, "model_copy"):
        settings = settings.model_copy(update={"task_runner_consumer_enabled": True})
    else:
        object.__setattr__(settings, "task_runner_consumer_enabled", True)

    await app_state.init(settings)
    runner = app_state.background_runner
    if not isinstance(runner, ArqTaskRunner):
        logger.error("arq / redis task runner not configured")
        await app_state.close()
        return 1

    if not runner._started:
        await runner.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())

    logger.info(
        "memory-worker running (arq queue=%s, dlq=%s)",
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
