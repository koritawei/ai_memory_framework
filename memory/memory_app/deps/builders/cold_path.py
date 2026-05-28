"""ColdPathServiceBuilder —— 冷路径 异步冷路径装配。"""

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
        from memory_app.background import BackgroundTaskRunner
        from memory_app.pipelines import ColdPathPipeline
        from memory_app.repositories.dlq import InMemoryDLQ
        from memory_app.services import ColdPathService, IngestService

        assert state.plugin_factory is not None  # noqa: S101

        # 1. LLM Provider —— 必须可达;不可达跳过整个冷路径
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

        # 2. Episode / Semantic / Clusterer —— 任一缺失即视为 冷路径 未启用
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

        # 3. 给 LLM 抽取器注入 LLM client(铁律:不直接 import provider 类)
        binder = DependencyBinder(llm_client=llm_provider)
        for extractor in (episode_extractor, semantic_extractor):
            binder.bind(extractor)

        # 4. BackgroundTaskRunner —— DLQ 复用 IngestService 的(若已建)
        if state.dlq is None:
            state.dlq = InMemoryDLQ()
        # 已存在的 runner 不覆盖,避免别的 builder 已经把任务接到旧 runner 上
        # 之后旧 runner 失去引用、未 shutdown 即 GC 造成任务静默丢失
        if state.background_runner is None:
            state.background_runner = BackgroundTaskRunner(dlq=state.dlq)

        # 5. ColdPathPipeline + Service
        pipeline = ColdPathPipeline(
            episode_extractor=episode_extractor,
            semantic_extractor=semantic_extractor,
            clusterer=clusterer,
        )
        state.cold_path_service = ColdPathService(
            pipeline=pipeline, runner=state.background_runner
        )

        # 6. 把 cold_path_service 接到既有 IngestService 上(经公开 attach_cold_path)
        if isinstance(state.ingest_service, IngestService):
            state.ingest_service.attach_cold_path(state.cold_path_service)

        logger.info(
            "cold_path_service initialized: episode=%s, semantic=%s, clusterer=%s, llm=%s",
            episode_extractor.meta.name,
            semantic_extractor.meta.name,
            clusterer.meta.name,
            llm_provider.meta.name,
        )


__all__ = ["ColdPathServiceBuilder"]
