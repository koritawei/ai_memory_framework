"""``graph_traversal`` —— 图与实体 图遍历召回插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.retrieval_channel.RetrievalChannel` 的图遍历
默认实现。委托 :class:`memory_app.retrieval.channels.graph.GraphChannel`。

═══════════════════════════════════════════════════════════════════════════════
依赖注入
═══════════════════════════════════════════════════════════════════════════════
- ``bind_memory_graph(g)``         :class:`MemoryGraph`(基于 GraphStore SPI)
- ``bind_mongo_repo(repo)``        :class:`MongoMemCellRepo`
- ``bind_entity_extractor(ext)``   ``EntityExtractor`` SPI(可选)
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.graph_index import MemoryGraph
from memory_app.internal_models import RankedMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.retrieval_channel import (
    RetrievalChannel,
    RetrievalContext,
)
from memory_app.retrieval.channels.graph import GraphChannel

logger = logging.getLogger(__name__)


@register
class GraphTraversalChannel(RetrievalChannel):
    """图遍历召回插件。"""

    meta = PluginMeta(
        name="graph_traversal",
        category="memory.retrieval.channels.graph",
        version="1.0.0",
        description="图遍历召回(BFS, max_depth=2);图与实体 默认 enabled=false",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "max_depth": {
                    "type": "integer", "minimum": 1, "maximum": 3, "default": 2
                },
            },
        },
    )

    def __init__(self) -> None:
        self._max_depth: int = 2
        self._memory_graph: MemoryGraph | None = None
        self._mongo_repo: Any = None
        self._entity_extractor: Any = None
        self._core: GraphChannel = GraphChannel()

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._max_depth = max(1, min(3, int(config.get("max_depth", 2))))
        self._rebuild_core()
        logger.info("graph_traversal started: max_depth=%d", self._max_depth)

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        ok = self._memory_graph is not None and self._mongo_repo is not None
        return {
            "status": "ok" if ok else "degraded",
            "detail": (
                f"graph={'bound' if self._memory_graph else 'unbound'}, "
                f"mongo={'bound' if self._mongo_repo else 'unbound'}, "
                f"max_depth={self._max_depth}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # 注入
    # ────────────────────────────────────────────────────────────────────────
    def bind_memory_graph(self, graph: MemoryGraph) -> None:
        self._memory_graph = graph
        self._rebuild_core()

    def bind_mongo_repo(self, repo: Any) -> None:
        self._mongo_repo = repo
        self._rebuild_core()

    def bind_entity_extractor(self, ext: Any) -> None:
        self._entity_extractor = ext
        self._rebuild_core()

    def _rebuild_core(self) -> None:
        self._core = GraphChannel(
            memory_graph=self._memory_graph,
            mongo_repo=self._mongo_repo,
            entity_extractor=self._entity_extractor,
            max_depth=self._max_depth,
        )

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    @property
    def channel_name(self) -> str:
        return "graph"

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


__all__ = ["GraphTraversalChannel"]
