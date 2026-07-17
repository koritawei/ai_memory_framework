"""``vector_milvus`` —— Phase 4 Step 4.2 向量召回插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.retrieval_channel.RetrievalChannel` 的默认向量
实现。委托 :class:`memory_app.retrieval.channels.vector.VectorChannel`,负责
满足 SPI 生命周期 + 注入 Milvus collection 与 EmbeddingProvider。
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
from memory_app.retrieval.channels.vector import VectorChannel

logger = logging.getLogger(__name__)


@register
class VectorMilvusChannel(RetrievalChannel):
    """Milvus 向量召回(Phase 4 默认)。"""

    meta = PluginMeta(
        name="vector_milvus",
        category="memory.retrieval.channels.vector",
        version="1.0.0",
        description="基于 Milvus 的稠密向量召回(COSINE)",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "anns_field": {"type": "string", "default": "embedding"},
                "metric_type": {
                    "type": "string",
                    "enum": ["COSINE", "IP", "L2"],
                    "default": "COSINE",
                },
                "nprobe": {"type": "integer", "minimum": 1, "maximum": 1024, "default": 16},
                "over_fetch_factor": {
                    "type": "integer", "minimum": 1, "maximum": 50, "default": 4
                },
                "output_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["mem_cell_id", "text", "memory_type"],
                },
            },
        },
    )

    def __init__(self) -> None:
        self._anns_field: str = "embedding"
        self._metric_type: str = "COSINE"
        self._nprobe: int = 16
        self._over_fetch_factor: int = 4
        self._output_fields: list[str] = ["mem_cell_id", "text", "memory_type"]
        self._collection: Any = None
        self._embedding_client: Any = None
        self._core: VectorChannel = VectorChannel()

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._anns_field = str(config.get("anns_field", "embedding"))
        self._metric_type = str(config.get("metric_type", "COSINE")).upper()
        self._nprobe = int(config.get("nprobe", 16))
        self._over_fetch_factor = int(config.get("over_fetch_factor", 4))
        self._output_fields = list(
            config.get("output_fields") or ["mem_cell_id", "text", "memory_type"]
        )
        self._rebuild_core()
        logger.info(
            "vector_milvus started: metric=%s, nprobe=%d, over_fetch=%d",
            self._metric_type, self._nprobe, self._over_fetch_factor,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        ok = self._collection is not None and self._embedding_client is not None
        return {
            "status": "ok" if ok else "degraded",
            "detail": (
                f"collection={'bound' if self._collection is not None else 'unbound'}, "
                f"embedding={'bound' if self._embedding_client is not None else 'unbound'}, "
                f"metric={self._metric_type}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # client 注入
    # ────────────────────────────────────────────────────────────────────────
    def bind_collection(self, collection: Any) -> None:
        self._collection = collection
        self._rebuild_core()

    def bind_embedding_client(self, client: Any) -> None:
        self._embedding_client = client
        self._rebuild_core()

    def _rebuild_core(self) -> None:
        self._core = VectorChannel(
            collection=self._collection,
            embedding_client=self._embedding_client,
            anns_field=self._anns_field,
            metric_type=self._metric_type,
            nprobe=self._nprobe,
            over_fetch_factor=self._over_fetch_factor,
            output_fields=self._output_fields,
        )

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    @property
    def channel_name(self) -> str:
        return "vector"

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


__all__ = ["VectorMilvusChannel"]
