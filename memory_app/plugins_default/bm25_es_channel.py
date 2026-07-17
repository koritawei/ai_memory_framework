"""``bm25_es`` —— Phase 4 Step 4.1 BM25 召回插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.retrieval_channel.RetrievalChannel` 的默认 BM25
实现。委托 :class:`memory_app.retrieval.channels.bm25.BM25Channel` 的核心算法,
负责满足 SPI 生命周期 + 注入 ES 客户端。

═══════════════════════════════════════════════════════════════════════════════
ES client 注入
═══════════════════════════════════════════════════════════════════════════════
ConfigCenter ``params`` 不含 client 实例。生产装配:
``deps._init_retrieval_orchestrator`` 在 ``factory.build("memory.retrieval.channels.bm25")``
之后调 :meth:`bind_es_client(es_client)`。
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
from memory_app.retrieval.channels.bm25 import BM25Channel

logger = logging.getLogger(__name__)


@register
class BM25ESChannel(RetrievalChannel):
    """ES BM25 召回(Phase 4 默认)。"""

    meta = PluginMeta(
        name="bm25_es",
        category="memory.retrieval.channels.bm25",
        version="1.0.0",
        description="基于 Elasticsearch 的 BM25 关键词召回",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "index_name": {"type": "string", "default": "memory_mem_cells"},
                "text_field": {"type": "string", "default": "text"},
                "over_fetch_factor": {
                    "type": "integer", "minimum": 1, "maximum": 50, "default": 4
                },
            },
        },
    )

    def __init__(self) -> None:
        self._core: BM25Channel = BM25Channel()
        self._index_name: str = "memory_mem_cells"
        self._text_field: str = "text"
        self._over_fetch_factor: int = 4

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._index_name = str(config.get("index_name", "memory_mem_cells"))
        self._text_field = str(config.get("text_field", "text"))
        self._over_fetch_factor = int(config.get("over_fetch_factor", 4))
        self._rebuild_core(self._core.es_client)
        logger.info(
            "bm25_es started: index=%s, field=%s, over_fetch=%d",
            self._index_name, self._text_field, self._over_fetch_factor,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok" if self._core.es_client is not None else "degraded",
            "detail": (
                f"index={self._index_name}, field={self._text_field}, "
                f"client={'bound' if self._core.es_client is not None else 'unbound'}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # ES client 注入
    # ────────────────────────────────────────────────────────────────────────
    def bind_es_client(self, es_client: Any) -> None:
        self._rebuild_core(es_client)

    def _rebuild_core(self, es_client: Any) -> None:
        self._core = BM25Channel(
            es_client=es_client,
            index_name=self._index_name,
            text_field=self._text_field,
            over_fetch_factor=self._over_fetch_factor,
        )

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    @property
    def channel_name(self) -> str:
        return "bm25"

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


__all__ = ["BM25ESChannel"]
