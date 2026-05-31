"""ConsolidationServiceBuilder —— Phase 6 离线巩固装配。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from memory_app.deps.builders.base import ServiceBuilder
from memory_app.plugins import DependencyBinder

if TYPE_CHECKING:
    from memory_app.deps.state import AppState

logger = logging.getLogger(__name__)


class ConsolidationServiceBuilder(ServiceBuilder):
    name: ClassVar[str] = "consolidation_service"
    requires: ClassVar[tuple[str, ...]] = (
        "plugin_factory",
        "clients.mongo_db",
    )

    async def build(self, state: "AppState") -> None:
        from memory_app.consolidation.decay import DecayManager
        from memory_app.consolidation.sleep import SleepConsolidator
        from memory_app.repositories.mongo_repo import MongoMemCellRepo
        from memory_app.services import ConsolidationService

        assert state.plugin_factory is not None  # noqa: S101

        # 1. Consolidator(必需)
        try:
            consolidator = await state.plugin_factory.build(
                "memory.lifecycle.consolidator"
            )
        except LookupError:
            logger.warning(
                "consolidation skipped: memory.lifecycle.consolidator not configured"
            )
            return

        # embedding 注入(可选)
        embedding_provider = await self._safe_build(
            state, "memory.provider.embedding"
        )
        DependencyBinder(embedding_client=embedding_provider).bind(consolidator)
        state.consolidator = consolidator

        # 2. ConsolidationStrategy(必需)
        try:
            strategy = await state.plugin_factory.build(
                "memory.lifecycle.consolidation_strategy"
            )
        except LookupError:
            logger.warning(
                "consolidation skipped: consolidation_strategy not configured"
            )
            return

        # 3. CapacityOptimizer(可选)
        cap_opt = await self._safe_build(state, "memory.lifecycle.capacity_optimizer")
        if cap_opt is not None:
            state.capacity_optimizer = cap_opt
        else:
            logger.debug(
                "capacity_optimizer not configured; using DecayManager fallback"
            )

        # 4. SleepConsolidator(LLM Provider 可达时启用)
        llm_provider = await self._safe_build(state, "memory.provider.llm")
        mongo_repo = MongoMemCellRepo(state.clients.mongo_db)
        sleep: SleepConsolidator | None = None
        if llm_provider is not None:
            sleep = SleepConsolidator(
                llm_client=llm_provider,
                mongo_repo=mongo_repo,
                consolidator=consolidator,
            )

        # 5. DecayManager(依赖 importance_scorer)
        scorer = state.importance_scorer
        if scorer is None:
            scorer = await self._safe_build(
                state, "memory.lifecycle.importance_scorer"
            )
            if scorer is not None:
                state.importance_scorer = scorer
        decay = (
            DecayManager(mongo_repo=mongo_repo, scorer=scorer) if scorer else None
        )

        # 6. 把组件注入 Strategy(关键字参数 bind 走静态方法)
        DependencyBinder.bind_pipeline_components(strategy, sleep=sleep, decay=decay)

        # 7. 装配 Service
        state.consolidation_service = ConsolidationService(strategy=strategy)
        logger.info(
            "consolidation_service initialized: strategy=%s, sleep=%s, decay=%s, capacity=%s",
            strategy.meta.name,
            "ok" if sleep else "off",
            "ok" if decay else "off",
            "ok" if state.capacity_optimizer else "off",
        )

    @staticmethod
    async def _safe_build(state: "AppState", category: str):
        """build 时把 LookupError 收敛为 None,其它异常重抛。"""
        assert state.plugin_factory is not None  # noqa: S101
        try:
            return await state.plugin_factory.build(category)
        except LookupError:
            return None


__all__ = ["ConsolidationServiceBuilder"]
