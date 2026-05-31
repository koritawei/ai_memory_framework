"""ColdPathServiceBuilder —— Phase 3 异步冷路径装配。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from memory_app.deps.builders.base import ServiceBuilder
from memory_app.plugins import DependencyBinder

if TYPE_CHECKING:
    from memory_app.deps.state import AppState

logger = logging.getLogger(__name__)


class ColdPathServiceBuilder(ServiceBuilder):
    name: ClassVar[str] = "cold_path_service"
    requires: ClassVar[tuple[str, ...]] = ("plugin_factory",)

    async def build(self, state: "AppState") -> None:
        from memory_app.pipelines import ColdPathPipeline
        from memory_app.repositories.dlq_factory import create_dlq
        from memory_app.repositories.mongo_repo import MongoMemCellRepo
        from memory_app.services import ColdPathService, IngestService
        from memory_app.task_queue.factory import create_task_runner
        from memory_app.task_queue.redis_runner import RedisTaskRunner

        assert state.plugin_factory is not None  # noqa: S101

        try:
            llm_provider = await state.plugin_factory.build("memory.provider.llm")
        except LookupError:
            logger.warning(
                "cold path skipped: memory.provider.llm not configured "
                "(set memory.provider.llm.name in default.yaml or override)"
            )
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("cold path skipped: llm provider unavailable: %s", e)
            return

        try:
            episode_extractor = await state.plugin_factory.build(
                "memory.generation.episode_extractor"
            )
            semantic_extractor = await state.plugin_factory.build(
                "memory.generation.semantic_extractor"
            )
            clusterer = await state.plugin_factory.build("memory.generation.clusterer")
        except LookupError as e:
            logger.warning("cold path skipped: missing plugin: %s", e)
            return

        binder = DependencyBinder(llm_client=llm_provider)
        for extractor in (episode_extractor, semantic_extractor):
            binder.bind(extractor)

        if state.dlq is None:
            state.dlq = await create_dlq(state.settings, state.clients)
        if state.background_runner is None:
            state.background_runner = await create_task_runner(
                state.settings, state.clients, state.dlq
            )

        pipeline = ColdPathPipeline(
            episode_extractor=episode_extractor,
            semantic_extractor=semantic_extractor,
            clusterer=clusterer,
        )
        state.cold_path_service = ColdPathService(
            pipeline=pipeline, runner=state.background_runner
        )

        if isinstance(state.background_runner, RedisTaskRunner):
            mongo_repo = state.mongo_repo
            if mongo_repo is None and state.clients.mongo_db is not None:
                mongo_repo = MongoMemCellRepo(state.clients.mongo_db)
                state.mongo_repo = mongo_repo

            async def _cold_path_handler(payload: dict) -> None:
                mem_cell_id = payload.get("mem_cell_id", "")
                if not mem_cell_id or mongo_repo is None:
                    return
                cell = await mongo_repo.get_by_id(mem_cell_id)
                if cell is None:
                    logger.warning("cold_path redis skip missing cell %s", mem_cell_id)
                    return
                await state.cold_path_service.run_now(cell)

            state.background_runner.register_handler("cold_path", _cold_path_handler)

        if isinstance(state.ingest_service, IngestService):
            state.ingest_service.attach_cold_path(state.cold_path_service)

        logger.info(
            "cold_path_service initialized: episode=%s, semantic=%s, clusterer=%s, "
            "llm=%s, runner=%s",
            episode_extractor.meta.name,
            semantic_extractor.meta.name,
            clusterer.meta.name,
            llm_provider.meta.name,
            state.settings.task_runner_backend,
        )


__all__ = ["ColdPathServiceBuilder"]
