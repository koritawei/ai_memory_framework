"""IngestServiceBuilder —— Phase 2 写入热路径门面装配。

铁律:
- SBD 必须经 ``factory.build(...)`` 取得,**禁止**直接 import 默认插件
- ES / Milvus 不可达时降级到 ``None``,SyncIndexStage 自动跳过
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from memory_app.deps.builders.base import ServiceBuilder

if TYPE_CHECKING:
    from memory_app.deps.state import AppState

logger = logging.getLogger(__name__)


class IngestServiceBuilder(ServiceBuilder):
    name: ClassVar[str] = "ingest_service"
    requires: ClassVar[tuple[str, ...]] = (
        "plugin_factory",
        "config_center",
        "clients.mongo_db",
    )

    async def build(self, state: "AppState") -> None:
        from memory_app.pipelines import IngestPipeline
        from memory_app.repositories.es_repo import ESMemCellRepo
        from memory_app.repositories.milvus_repo import MilvusMemCellRepo
        from memory_app.repositories.mongo_repo import MongoMemCellRepo
        from memory_app.services import IngestService

        assert state.plugin_factory is not None  # noqa: S101 —— can_build 已守卫
        # settings 不在 requires 里:它由 AppState.init 在 BUILDERS 循环前
        # 无条件设置(见 deps/state.py::init);assert 是文档性兜底。
        assert state.settings is not None  # noqa: S101

        # 1. SBD 走插件路径(铁律)
        sbd = await state.plugin_factory.build(
            "memory.generation.boundary_detector"
        )

        # 2. 主存(SOT)— MongoDB
        mongo_repo = MongoMemCellRepo(state.clients.mongo_db)
        await mongo_repo.ensure_indexes()  # 启动期幂等建索引

        # 3. 从属索引(可降级)
        es_repo = (
            ESMemCellRepo(
                state.clients.es_client,
                index_prefix=state.settings.es_index_prefix,
            )
            if state.clients.es_client is not None
            else None
        )
        if es_repo is not None:
            await es_repo.ensure_index()
        milvus_repo = (
            MilvusMemCellRepo(state.settings.milvus_collection)
            if state.clients.milvus_connected
            else None
        )

        # 4. DLQ —— 按 bootstrap 选择持久化后端
        if state.dlq is None:
            from memory_app.repositories.dlq_factory import create_dlq

            state.dlq = await create_dlq(state.settings, state.clients)

        # 5. 装配管线 + 服务(冷路径在 ColdPathServiceBuilder 后挂接)
        pipeline = IngestPipeline(
            segmenter=sbd,
            mem_cell_repo=mongo_repo,
            es_repo=es_repo,
            milvus_repo=milvus_repo,
            dlq=state.dlq,
        )
        state.mongo_repo = mongo_repo
        state.ingest_service = IngestService(pipeline)
        logger.info(
            "ingest_service initialized: sbd=%s, es=%s, milvus=%s, dlq=%s",
            sbd.meta.name,
            "ok" if es_repo else "off",
            "ok" if milvus_repo else "off",
            state.settings.dlq_backend,
        )


__all__ = ["IngestServiceBuilder"]
