"""EntityChannel —— Entity Boost 召回通道。

═══════════════════════════════════════════════════════════════════════════════
流程
═══════════════════════════════════════════════════════════════════════════════
1. 从 ``query`` 抽取实体(``EntityExtractor`` SPI;无则简单分词)
2. ``EntityStore.find_by_entities`` 取关联 ``mem_cell_id`` 集合
3. 通过 ``MongoMemCellRepo.get_by_id`` 拿回完整 MemCell
4. 按"实体匹配数 / 总实体数"算分,降序排序输出 :class:`RankedMemory`

═══════════════════════════════════════════════════════════════════════════════
分数计算
═══════════════════════════════════════════════════════════════════════════════
``score = matched_count / total_query_entities``,落在 ``[0, 1]``;由 RRF 标准化
吸收尺度差异。
"""

from __future__ import annotations

import logging
from typing import Any

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.retrieval.channels.base import BaseRetrievalChannel

logger = logging.getLogger(__name__)


class EntityChannel(BaseRetrievalChannel):
    """实体倒排索引召回。"""

    channel_name = "entity"

    def __init__(
        self,
        entity_store: Any | None = None,
        mongo_repo: Any | None = None,
        entity_extractor: Any | None = None,
        *,
        top_k_lookup: int = 200,
    ) -> None:
        self.entity_store = entity_store
        self.mongo_repo = mongo_repo
        self.entity_extractor = entity_extractor
        self.top_k_lookup = max(1, int(top_k_lookup))

    # ────────────────────────────────────────────────────────────────────────
    # 依赖
    # ────────────────────────────────────────────────────────────────────────
    def _check_dependencies(self) -> None:
        if self.entity_store is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "entity_store_unset",
                "EntityChannel: entity_store not set",
                retryable=True,
            )
        if self.mongo_repo is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "mongo_repo_unset",
                "EntityChannel: mongo_repo not set",
                retryable=True,
            )

    # ────────────────────────────────────────────────────────────────────────
    # 调用
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
        # 1. 抽取实体
        entities = await self._extract_entities(query)
        if not entities:
            return {"hits": [], "entities": []}

        # 2. EntityStore 查
        mem_cell_ids = await self.entity_store.find_by_entities(
            entities, tenant_id, user_id, limit=self.top_k_lookup
        )
        if not mem_cell_ids:
            return {"hits": [], "entities": list(entities)}

        # 3. 取回 MemCell + 算分 —— 用 get_by_ids 替代 N 次 find_one,
        #    把 200 次 Mongo round-trip 压成 1 次 $in 查询。
        cells = await self._fetch_cells(mem_cell_ids)
        if not cells:
            return {"hits": [], "entities": list(entities)}
        scored: list[tuple[float, Any]] = []
        total_q = max(1, len(entities))
        for cell in cells:
            text = cell.text or ""
            matched = sum(1 for e in entities if e in text)
            if matched == 0:
                # 实体在 EntityStore 命中但不在 text 中(可能 indexer/text 未同步)
                # 仍给予一个最低分,避免完全过滤
                matched = 1
            scored.append((matched / total_q, cell))
        scored.sort(key=lambda t: t[0], reverse=True)
        return {"hits": scored[: top_k], "entities": list(entities)}

    async def _fetch_cells(self, ids: list[str]) -> list:
        """优先批量取(``get_by_ids``);老版 repo 无该方法时退化到 gather。"""
        batch_fn = getattr(self.mongo_repo, "get_by_ids", None)
        if callable(batch_fn):
            return await batch_fn(ids)
        import asyncio
        results = await asyncio.gather(
            *[self.mongo_repo.get_by_id(m) for m in ids],
            return_exceptions=False,
        )
        return [c for c in results if c is not None]

    # ────────────────────────────────────────────────────────────────────────
    # 解析
    # ────────────────────────────────────────────────────────────────────────
    def _parse_hits(self, raw: Any) -> list[RankedMemory]:
        if not raw:
            return []
        hits = raw.get("hits", [])
        out: list[RankedMemory] = []
        for score, cell in hits:
            md = {
                "matched_entities": list(raw.get("entities", [])),
                "tenant_id": cell.tenant_id,
                "user_id": cell.user_id,
            }
            out.append(
                RankedMemory(
                    memory_id=cell.mem_cell_id,
                    memory_type=MemoryType.EPISODIC,
                    content=cell.text or "",
                    score=float(score),
                    source_channel=self.channel_name,
                    metadata=md,
                )
            )
        return out

    # ────────────────────────────────────────────────────────────────────────
    # 实体抽取
    # ────────────────────────────────────────────────────────────────────────
    async def _extract_entities(self, query: str) -> list[str]:
        if self.entity_extractor is None:
            return _fallback_tokenize(query)
        try:
            entities = await self.entity_extractor.extract(query)
        except Exception as e:  # noqa: BLE001
            logger.warning("entity_extractor failed (fallback to tokenize): %s", e)
            return _fallback_tokenize(query)
        out: list[str] = []
        seen: set[str] = set()
        for e in entities or []:
            text = getattr(e, "text", None) or str(e)
            text = text.strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                out.append(text)
        return out


# ════════════════════════════════════════════════════════════════════════════
# fallback 简单分词
# ════════════════════════════════════════════════════════════════════════════
def _fallback_tokenize(query: str) -> list[str]:
    """对查询做最简单的分词:CJK 段(≥2 字)+ 英文 token(≥3 字)。"""
    import re

    if not query or not query.strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[一-鿿]{2,}|[A-Za-z]{3,}", query):
        t = token.strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
    return out


__all__ = ["EntityChannel"]
