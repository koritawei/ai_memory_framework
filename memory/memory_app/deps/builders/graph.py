"""GraphComponentsBuilder —— 图与实体 实体 / 图与对应检索通道装配。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from memory_app.deps.builders.base import ServiceBuilder
from memory_app.plugins import DependencyBinder

if TYPE_CHECKING:
    from memory_app.deps.state import AppState

logger = logging.getLogger(__name__)


class GraphComponentsBuilder(ServiceBuilder):
    name: ClassVar[str] = "graph_components"
    requires: ClassVar[tuple[str, ...]] = ("plugin_factory",)

    async def build(self, state: "AppState") -> None:
        from memory_app.entity_store import EntityStore, InMemoryEntityStore
        from memory_app.graph_index import MemoryGraph
        from memory_app.repositories.mongo_repo import MongoMemCellRepo

        assert state.plugin_factory is not None  # noqa: S101

        # 1. 共享 Mongo repo(供 query / channels 使用)
        if state.clients.mongo_db is not None and state.mongo_repo is None:
            state.mongo_repo = MongoMemCellRepo(state.clients.mongo_db)

        # 2. EntityStore:Mongo 后端 → fallback InMemoryEntityStore
        if state.clients.mongo_db is not None:
            es = EntityStore(state.clients.mongo_db)
            try:
                await es.ensure_indexes()
            except Exception as e:  # noqa: BLE001
                logger.warning("entity_store ensure_indexes degraded: %s", e)
            state.entity_store = es
        else:
            logger.info("entity_store: using InMemoryEntityStore (no mongo)")
            state.entity_store = InMemoryEntityStore()

        # 3. EntityExtractor(SPI 插件,可选)
        try:
            state.entity_extractor = await state.plugin_factory.build(
                "memory.generation.entity_extractor"
            )
        except LookupError:
            logger.debug(
                "entity_extractor not configured (channels will fallback to tokenize)"
            )
            state.entity_extractor = None
        except Exception as e:  # noqa: BLE001
            logger.warning("entity_extractor build failed: %s", e)
            state.entity_extractor = None

        # 4. GraphStore + MemoryGraph
        graph_store = None
        try:
            graph_store = await state.plugin_factory.build("memory.storage.graph_store")
        except LookupError:
            logger.debug("graph_store not configured")
        except Exception as e:  # noqa: BLE001
            logger.warning("graph_store build failed: %s", e)
        if graph_store is not None:
            state.memory_graph = MemoryGraph(graph_store)

        # 5. 把 Entity / Graph channels 接入 RetrievalOrchestrator(若已就绪)
        await self._wire_graph_channels(state)

        # 6. 把 EntityIndexStage 接入 ColdPathPipeline(若已就绪)
        self._wire_entity_index_cold_stage(state)

        logger.info(
            "图与实体 components: entity_store=%s, extractor=%s, graph=%s",
            type(state.entity_store).__name__ if state.entity_store else "off",
            getattr(getattr(state.entity_extractor, "meta", None), "name", "off"),
            "ok" if state.memory_graph else "off",
        )

    # ────────────────────────────────────────────────────────────────────────
    # 子接线
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    async def _wire_graph_channels(state: "AppState") -> None:
        """根据 config 中各 channel.enabled 决定是否装配 entity / graph 通道。

        通过 RetrievalOrchestrator.add_recall_channel(公开 API)接入,
        避免直接 reach orchestrator._recall(私有字段)。
        """
        if state.retrieval_orchestrator is None or state.plugin_factory is None:
            return
        # 若 orchestrator 没暴露 add_recall_channel,说明上游不支持挂通道
        add_channel = getattr(
            state.retrieval_orchestrator, "add_recall_channel", None
        )
        if not callable(add_channel):
            logger.debug(
                "orchestrator has no add_recall_channel; skip channel inject"
            )
            return

        graph_channel_binder = DependencyBinder(
            entity_store=state.entity_store,
            mongo_repo=state.mongo_repo,
            entity_extractor=state.entity_extractor,
            memory_graph=state.memory_graph,
        )

        for slug in ("entity", "graph"):
            channel = None
            try:
                channel = await state.plugin_factory.build(
                    f"memory.retrieval.channels.{slug}"
                )
            except LookupError:
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning("%s channel build failed: %s", slug, e)
                continue
            if channel is None:
                continue
            graph_channel_binder.bind(channel)
            if add_channel(slug, channel):
                logger.info("injected retrieval channel: %s", slug)
            else:
                logger.debug(
                    "orchestrator declined add_recall_channel for %s", slug
                )

    @staticmethod
    def _wire_entity_index_cold_stage(state: "AppState") -> None:
        """把 EntityIndexStage 注入既有 ColdPathPipeline(若已就绪)。

        通过 ColdPathService.attach_stage / find_extra_stage(公开 API)接入,
        不再 reach 私有 ``_pipeline._extra_stages``。
        """
        from memory_app.pipelines import EntityIndexStage

        if state.cold_path_service is None:
            return
        if state.entity_store is None and state.memory_graph is None:
            return
        # 通过 service 公开 API 操作(decouple from pipeline internals)
        attach_stage = getattr(state.cold_path_service, "attach_stage", None)
        find_stage = getattr(state.cold_path_service, "find_extra_stage", None)
        if not (callable(attach_stage) and callable(find_stage)):
            logger.debug(
                "cold_path_service lacks attach_stage API; skip entity index inject"
            )
            return
        # 幂等:已存在同类型 stage → 仅刷新绑定
        existing = find_stage(lambda s: isinstance(s, EntityIndexStage))
        if existing is not None:
            existing.bind_entity_store(state.entity_store)
            existing.bind_memory_graph(state.memory_graph)
            if state.entity_extractor is not None:
                existing.bind_entity_extractor(state.entity_extractor)
            logger.debug("entity_index stage rebound (idempotent)")
            return
        stage = EntityIndexStage(
            entity_store=state.entity_store,
            memory_graph=state.memory_graph,
            entity_extractor=state.entity_extractor,
        )
        attach_stage(stage)
        logger.info("injected cold path stage: entity_index")


__all__ = ["GraphComponentsBuilder"]
