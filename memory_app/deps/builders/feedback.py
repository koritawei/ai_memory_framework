"""FeedbackLifecycleBuilder —— Phase 5 反馈 + 生命周期 + 重要性评分装配。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from memory_app.deps.builders.base import ServiceBuilder, shared_mongo_repo

if TYPE_CHECKING:
    from memory_app.deps.state import AppState

logger = logging.getLogger(__name__)


class FeedbackLifecycleBuilder(ServiceBuilder):
    name: ClassVar[str] = "feedback_service"
    requires: ClassVar[tuple[str, ...]] = (
        "plugin_factory",
        "clients.mongo_db",
    )

    async def build(self, state: "AppState") -> None:
        from memory_app.lifecycle import LifecycleUpdater
        from memory_app.services import FeedbackService

        assert state.plugin_factory is not None  # noqa: S101

        # Reinforcer(必需)
        try:
            reinforcer = await state.plugin_factory.build(
                "memory.lifecycle.reinforcer"
            )
        except LookupError:
            logger.warning(
                "feedback service skipped: memory.lifecycle.reinforcer not configured"
            )
            return

        mongo_repo = await shared_mongo_repo(state)

        state.feedback_service = FeedbackService(
            mongo_repo=mongo_repo, reinforcer=reinforcer
        )

        # LifecycleUpdater(Step 5.2):基于 BackgroundTaskRunner 触发(可无)
        state.lifecycle_updater = LifecycleUpdater(
            mongo_repo=mongo_repo, runner=state.background_runner
        )

        # ImportanceScorer(Step 5.3):可选
        try:
            state.importance_scorer = await state.plugin_factory.build(
                "memory.lifecycle.importance_scorer"
            )
        except LookupError:
            logger.debug("importance_scorer not configured (Phase 5 optional)")
        except Exception as e:  # noqa: BLE001
            logger.warning("importance_scorer build failed: %s", e)

        # 经公开 add_finalize_hook 把 LifecycleUpdater 接入既有 RetrievalOrchestrator
        # 替代旧版 monkey-patch ``orch.finalize = wrapped_finalize`` 反模式
        if state.retrieval_orchestrator is not None and state.background_runner is not None:
            self._wire_retrieval_finalize_hook(state)

        logger.info(
            "feedback_service initialized: reinforcer=%s, lifecycle=%s, scorer=%s",
            reinforcer.meta.name,
            "ok" if state.lifecycle_updater else "off",
            getattr(getattr(state.importance_scorer, "meta", None), "name", "off"),
        )

    @staticmethod
    def _wire_retrieval_finalize_hook(state: "AppState") -> None:
        """把 lifecycle.on_retrieval_hit 注册为 orchestrator finalize 钩子。"""
        orch = state.retrieval_orchestrator
        updater = state.lifecycle_updater
        if orch is None or updater is None:
            return

        async def _hook(ctx, results: list) -> None:
            if not results:
                return
            ids = [m.memory_id for m in results]
            try:
                updater.on_retrieval_hit(
                    ids,
                    tenant_id=ctx.request.tenant_id,
                    user_id=ctx.request.user_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("lifecycle on_retrieval_hit failed: %s", e)

        # RetrievalOrchestrator 在 S2 引入 add_finalize_hook 公开 API
        add_hook = getattr(orch, "add_finalize_hook", None)
        if callable(add_hook):
            add_hook(_hook)
        else:
            logger.debug(
                "retrieval orchestrator missing add_finalize_hook; lifecycle hook skipped"
            )


__all__ = ["FeedbackLifecycleBuilder"]
