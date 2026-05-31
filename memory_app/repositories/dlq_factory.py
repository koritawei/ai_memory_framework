"""DLQ 后端工厂。"""

from __future__ import annotations

import logging
from typing import Any

from memory_app.repositories.dlq import InMemoryDLQ
from memory_app.repositories.mongo_dlq import MongoDLQ
from memory_app.repositories.redis_dlq import RedisDLQ, DEFAULT_REDIS_DLQ_KEY
from memory_app.settings import Settings

logger = logging.getLogger(__name__)


async def create_dlq(settings: Settings, clients: Any) -> Any:
    """按 ``settings.dlq_backend`` 构造 DLQ 实例。"""
    backend = settings.dlq_backend
    if backend == "mongo":
        if clients.mongo_db is None:
            logger.warning("dlq_backend=mongo but mongo unavailable; fallback memory")
            return InMemoryDLQ()
        dlq = MongoDLQ(clients.mongo_db)
        await dlq.ensure_indexes()
        logger.info("dlq initialized: mongo")
        return dlq
    if backend == "redis":
        if clients.redis_client is None:
            logger.warning("dlq_backend=redis but redis unavailable; fallback memory")
            return InMemoryDLQ()
        dlq = RedisDLQ(clients.redis_client, key=DEFAULT_REDIS_DLQ_KEY)
        logger.info("dlq initialized: redis key=%s", DEFAULT_REDIS_DLQ_KEY)
        return dlq
    logger.info("dlq initialized: in_memory")
    return InMemoryDLQ()


__all__ = ["create_dlq"]
