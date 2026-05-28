"""BM25Channel —— ES 关键词检索通道。

═══════════════════════════════════════════════════════════════════════════════
查询模型
═══════════════════════════════════════════════════════════════════════════════
::

    {
      "query": {
        "bool": {
          "must":   [{"match": {"text": "<query>"}}],
          "filter": [
            {"term": {"tenant_id": "..."}},
            {"term": {"user_id": "..."}}
          ]
        }
      },
      "size": top_k * over_fetch_factor
    }

═══════════════════════════════════════════════════════════════════════════════
解析
═══════════════════════════════════════════════════════════════════════════════
- ``hits.hits[*]._id``                 → ``memory_id``
- ``hits.hits[*]._score``              → ``score``
- ``hits.hits[*]._source.text``        → ``content``
- ``hits.hits[*]._source.{tenant_id, user_id, ...}`` → ``metadata``

::

    RankedMemory(
        memory_id=..., score=..., content=..., source_channel="bm25",
        memory_type=...   # 来源 _source.memory_type 或默认 EPISODIC
    )
"""

from __future__ import annotations

import logging
from typing import Any

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.retrieval.channels.base import BaseRetrievalChannel

logger = logging.getLogger(__name__)


class BM25Channel(BaseRetrievalChannel):
    """BM25 关键词召回(基于 Elasticsearch ``_score``)。"""

    channel_name = "bm25"

    def __init__(
        self,
        es_client: Any | None = None,
        *,
        index_name: str = "memory_mem_cells",
        text_field: str = "text",
        over_fetch_factor: int = 4,
    ) -> None:
        self.es_client = es_client
        self.index_name = index_name
        self.text_field = text_field
        self.over_fetch_factor = max(1, int(over_fetch_factor))

    # ────────────────────────────────────────────────────────────────────────
    # 依赖
    # ────────────────────────────────────────────────────────────────────────
    def _check_dependencies(self) -> None:
        if self.es_client is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "es_client_unset",
                "BM25Channel: es_client not set",
                retryable=True,
            )

    # ────────────────────────────────────────────────────────────────────────
    # 调 ES
    # ────────────────────────────────────────────────────────────────────────
    async def _execute_search(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int,
        filters: dict[str, Any],
    ) -> Any:
        body = {
            "query": {
                "bool": {
                    "must": [{"match": {self.text_field: query}}],
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"user_id": user_id}},
                    ],
                }
            },
            "size": top_k * self.over_fetch_factor,
        }
        # 结构化过滤(time_range / memory_type 等)
        for k, v in filters.items():
            if v is None:
                continue
            body["query"]["bool"]["filter"].append({"term": {k: v}})
        return await self.es_client.search(index=self.index_name, body=body)

    # ────────────────────────────────────────────────────────────────────────
    # 解析
    # ────────────────────────────────────────────────────────────────────────
    def _parse_hits(self, raw: Any) -> list[RankedMemory]:
        if not raw:
            return []
        hits_obj = raw.get("hits") if isinstance(raw, dict) else None
        if not hits_obj:
            return []
        items = hits_obj.get("hits", [])
        out: list[RankedMemory] = []
        for h in items:
            source = h.get("_source") or {}
            doc_id = h.get("_id") or source.get("mem_cell_id") or ""
            text = source.get(self.text_field) or source.get("text") or ""
            try:
                score = float(h.get("_score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            mem_type = (
                _normalize_memory_type(source.get("memory_type"))
                or MemoryType.EPISODIC
            )
            md = {k: v for k, v in source.items() if k != self.text_field}
            out.append(
                RankedMemory(
                    memory_id=str(doc_id),
                    memory_type=mem_type,
                    content=text,
                    score=score,
                    source_channel=self.channel_name,
                    metadata=md,
                )
            )
        return out


def _normalize_memory_type(value: Any) -> MemoryType | None:
    if not value:
        return None
    try:
        return MemoryType(str(value).strip().upper())
    except ValueError:
        return None


__all__ = ["BM25Channel"]
