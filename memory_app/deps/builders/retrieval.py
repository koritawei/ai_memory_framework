"""RetrievalOrchestratorBuilder —— Phase 4 检索五阶段装配。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from memory_app.deps.builders.base import ServiceBuilder
from memory_app.plugins import DependencyBinder

if TYPE_CHECKING:
    from memory_app.deps.state import AppState

logger = logging.getLogger(__name__)


class RetrievalOrchestratorBuilder(ServiceBuilder):
    name: ClassVar[str] = "retrieval_orchestrator"
    requires: ClassVar[tuple[str, ...]] = ("plugin_factory",)

    async def build(self, state: "AppState") -> None:
        from memory_app.retrieval.orchestrator import RetrievalOrchestrator

        assert state.plugin_factory is not None  # noqa: S101

        # 1. fuser
        try:
            fuser = await state.plugin_factory.build("memory.retrieval.fuser")
        except LookupError:
            fuser = None
            logger.warning(
                "retrieval: fuser not configured (degraded to flat concat)"
            )

        # 2. reranker
        try:
            reranker = await state.plugin_factory.build("memory.retrieval.reranker")
        except LookupError:
            reranker = None
            logger.warning("retrieval: reranker not configured")

        # 3. filter(单一插件;Phase 4+ 可拓展为链)
        filters: list = []
        try:
            ft = await state.plugin_factory.build("memory.retrieval.filter")
            filters.append(ft)
        except LookupError:
            logger.debug("retrieval: filter not configured (no filtering)")

        # 4. embedding provider:用于 vector 通道
        embedding_provider: Any = None
        try:
            embedding_provider = await state.plugin_factory.build(
                "memory.provider.embedding"
            )
        except LookupError:
            logger.debug(
                "retrieval: memory.provider.embedding not configured "
                "(vector channel will skip)"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("retrieval: embedding provider unavailable: %s", e)

        # 5. channels —— 试图装配 bm25 / vector
        milvus_collection = self._materialize_milvus_collection(state)
        channel_binder = DependencyBinder(
            es_client=state.clients.es_client,
            embedding_client=embedding_provider,
            milvus_collection=milvus_collection,
        )

        channels: dict = {}
        for slug in ("bm25", "vector"):
            channel = await self._build_channel(state, slug)
            if channel is not None:
                channel_binder.bind(channel)
                channels[slug] = channel

        if not channels:
            logger.warning(
                "retrieval orchestrator skipped: no channel could be built"
            )
            return

        state.retrieval_orchestrator = RetrievalOrchestrator(
            channels=channels,
            fuser=fuser,
            filters=filters,
            reranker=reranker,
            over_fetch_factor=4,
        )
        logger.info(
            "retrieval orchestrator initialized: channels=%s, fuser=%s, "
            "reranker=%s, filters=%d",
            list(channels.keys()),
            getattr(getattr(fuser, "meta", None), "name", None),
            getattr(getattr(reranker, "meta", None), "name", None),
            len(filters),
        )

    # ────────────────────────────────────────────────────────────────────────
    # 辅助
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _materialize_milvus_collection(state: "AppState") -> Any | None:
        """vector 通道需要的 pymilvus Collection;失败仅 warn。"""
        if not state.clients.milvus_connected or state.settings is None:
            return None
        try:
            from pymilvus import Collection

            return Collection(state.settings.milvus_collection)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "retrieval: failed to materialize milvus collection: %s", e
            )
            return None

    @staticmethod
    async def _build_channel(state: "AppState", slug: str) -> Any | None:
        """``memory.retrieval.channels.{slug}`` build 失败时返回 None。"""
        assert state.plugin_factory is not None  # noqa: S101
        try:
            return await state.plugin_factory.build(
                f"memory.retrieval.channels.{slug}"
            )
        except LookupError:
            return None


__all__ = ["RetrievalOrchestratorBuilder"]
