"""``entity_boost`` —— Phase 7 Step 7.2 Entity Boost 召回插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.retrieval_channel.RetrievalChannel` 的实体召回
默认实现。委托 :class:`memory_app.retrieval.channels.entity.EntityChannel`。

═══════════════════════════════════════════════════════════════════════════════
依赖注入
═══════════════════════════════════════════════════════════════════════════════
- ``bind_entity_store(es)``       :class:`EntityStore`
- ``bind_mongo_repo(repo)``       :class:`MongoMemCellRepo`
- ``bind_entity_extractor(ext)``  ``EntityExtractor`` SPI 实例(可选;无则简单分词)
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.internal_models import RankedMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.retrieval_channel import (
    RetrievalChannel,
    RetrievalContext,
)
from memory_app.retrieval.channels.entity import EntityChannel

logger = logging.getLogger(__name__)


@register
class EntityBoostChannel(RetrievalChannel):
    """实体倒排索引召回插件。"""

    meta = PluginMeta(
        name="entity_boost",
        category="memory.retrieval.channels.entity",
        version="1.0.0",
        description="Entity Boost(倒排索引召回);Phase 7 默认 enabled=false",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "top_k_lookup": {
                    "type": "integer", "minimum": 10, "maximum": 5000, "default": 200
                },
            },
        },
    )

    def __init__(self) -> None:
        self._top_k_lookup: int = 200
        self._entity_store: Any = None
        self._mongo_repo: Any = None
        self._entity_extractor: Any = None
        self._core: EntityChannel = EntityChannel()

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._top_k_lookup = int(config.get("top_k_lookup", 200))
        self._rebuild_core()
        logger.info("entity_boost started: top_k_lookup=%d", self._top_k_lookup)

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        ok = self._entity_store is not None and self._mongo_repo is not None
        return {
            "status": "ok" if ok else "degraded",
            "detail": (
                f"entity_store={'bound' if self._entity_store else 'unbound'}, "
                f"mongo_repo={'bound' if self._mongo_repo else 'unbound'}, "
                f"extractor={'bound' if self._entity_extractor else 'unbound'}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # 注入
    # ────────────────────────────────────────────────────────────────────────
    def bind_entity_store(self, store: Any) -> None:
        self._entity_store = store
        self._rebuild_core()

    def bind_mongo_repo(self, repo: Any) -> None:
        self._mongo_repo = repo
        self._rebuild_core()

    def bind_entity_extractor(self, ext: Any) -> None:
        self._entity_extractor = ext
        self._rebuild_core()

    def _rebuild_core(self) -> None:
        self._core = EntityChannel(
            entity_store=self._entity_store,
            mongo_repo=self._mongo_repo,
            entity_extractor=self._entity_extractor,
            top_k_lookup=self._top_k_lookup,
        )

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    @property
    def channel_name(self) -> str:
        return "entity"

    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]:
        return await self._core.search(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            query=query,
            top_k=k,
            filters=ctx.filters,
        )


__all__ = ["EntityBoostChannel"]
