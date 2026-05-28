"""ExternalClients —— 外部依赖客户端组(Mongo / ES / Redis / Milvus)。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
原 ``deps.py`` 内 ``_init_mongo`` / ``_init_es`` / ``_init_redis`` /
``_init_milvus`` + ``close`` 中对各客户端的释放逻辑,本模块统一收口。

设计原则与原版一致:
- **延迟可达性**:任一客户端 init 失败仅 warn,字段保留为 ``None``
- **幂等**:重复 ``init`` 不会重建已就绪的客户端
- **优雅关闭**:``close`` 对每个子项独立 try/except
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

from memory_app.settings import Settings

logger = logging.getLogger(__name__)


class ExternalClients:
    """Mongo / ES / Redis / Milvus 客户端容器(可降级)。

    所有字段初始为 ``None``;:meth:`init` 后按可达情况赋值。
    """

    def __init__(self) -> None:
        self.mongo_client: Any = None
        self.mongo_db: Any = None
        self.es_client: Any = None
        self.redis_client: Any = None
        self.milvus_alias: str | None = None
        self._milvus_connected: bool = False

    @property
    def milvus_connected(self) -> bool:
        return self._milvus_connected

    # ════════════════════════════════════════════════════════════════════════
    # 启动
    # ════════════════════════════════════════════════════════════════════════
    async def init(self, settings: Settings) -> None:
        """按外部依赖顺序拉起客户端。所有失败仅 warn,字段保留 None。"""
        await self.init_mongo(settings)
        await self.init_es(settings)
        await self.init_redis(settings)
        self.init_milvus(settings)

    async def init_mongo(self, settings: Settings) -> None:
        """幂等:已就绪时直接返回(支持 ConfigCenter 提前拉起 Mongo 的场景)。"""
        if self.mongo_client is not None:
            return
        try:
            from motor.motor_asyncio import AsyncIOMotorClient

            # serverSelectionTimeoutMS=2000:避免本地无 Mongo 时启动卡 30s
            self.mongo_client = AsyncIOMotorClient(
                settings.mongo_uri, serverSelectionTimeoutMS=2000
            )
            self.mongo_db = self.mongo_client[settings.mongo_db]
            logger.info(
                "mongo client created (uri=%s db=%s)",
                settings.mongo_uri, settings.mongo_db,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("mongo client init failed (degraded): %s", e)

    async def init_es(self, settings: Settings) -> None:
        if self.es_client is not None:
            return
        try:
            from elasticsearch import AsyncElasticsearch

            self.es_client = AsyncElasticsearch(
                settings.es_hosts, request_timeout=2.0
            )
            logger.info("es client created (hosts=%s)", settings.es_hosts)
        except Exception as e:  # noqa: BLE001
            logger.warning("es client init failed (degraded): %s", e)

    async def init_redis(self, settings: Settings) -> None:
        if self.redis_client is not None:
            return
        try:
            from redis.asyncio import Redis

            self.redis_client = Redis.from_url(
                settings.redis_url, socket_connect_timeout=2.0
            )
            logger.info("redis client created (url=%s)", settings.redis_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("redis client init failed (degraded): %s", e)

    def init_milvus(self, settings: Settings) -> None:
        if self._milvus_connected:
            return
        try:
            from pymilvus import connections as milvus_conn

            with warnings.catch_warnings():
                # 抑制 PyMilvus 3.x 的 ORM 弃用 warning:保留 connections.connect
                # 是为了兼容 Milvus 2.x 服务端;升级到 MilvusClient 时再切。
                warnings.filterwarnings("ignore", message=r".*ORM-style PyMilvus.*")
                warnings.filterwarnings("ignore", category=DeprecationWarning)
                milvus_conn.connect(
                    alias="default",
                    host=settings.milvus_host,
                    port=str(settings.milvus_port),
                    timeout=2.0,
                )
            self.milvus_alias = "default"
            self._milvus_connected = True
            logger.info(
                "milvus connected (host=%s port=%d)",
                settings.milvus_host, settings.milvus_port,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("milvus connect failed (degraded): %s", e)

    # ════════════════════════════════════════════════════════════════════════
    # 关闭
    # ════════════════════════════════════════════════════════════════════════
    async def close(self) -> None:
        """优雅关闭。各子项独立 try/except。"""
        if self.mongo_client is not None:
            try:
                self.mongo_client.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("mongo close failed: %s", e)
        if self.es_client is not None:
            try:
                await self.es_client.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("es close failed: %s", e)
        if self.redis_client is not None:
            try:
                # Redis 异步客户端的关闭方法是 aclose(asyncio 5.x+)
                await self.redis_client.aclose()
            except Exception as e:  # noqa: BLE001
                logger.warning("redis close failed: %s", e)
        if self._milvus_connected:
            try:
                from pymilvus import connections as milvus_conn

                milvus_conn.disconnect("default")
            except Exception as e:  # noqa: BLE001
                logger.warning("milvus disconnect failed: %s", e)


__all__ = ["ExternalClients"]
