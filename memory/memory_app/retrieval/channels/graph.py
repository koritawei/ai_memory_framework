"""GraphChannel —— 图遍历召回通道。

═══════════════════════════════════════════════════════════════════════════════
流程
═══════════════════════════════════════════════════════════════════════════════
1. 从 ``query`` 抽取实体(``EntityExtractor`` SPI;无则简单分词)
2. 把每个实体转 ``entity:{tenant}:{user}:{name}`` 节点
3. ``MemoryGraph.get_neighbors``(BFS,默认 2 跳)拿邻域内 memory 节点
4. 通过 :class:`MongoMemCellRepo` 拿回 MemCell,组装 :class:`RankedMemory`

═══════════════════════════════════════════════════════════════════════════════
分数计算
═══════════════════════════════════════════════════════════════════════════════
- 1 跳命中 → ``1.0``
- 2 跳命中 → ``0.6``(衰减)
- N 跳命中 → ``0.6 ** (hop-1)``

具体跳数由 store 的 traverse 不暴露明细;图与实体 简化:统一给 1.0,管理面 起
通过 GraphStore 扩展 traverse 返回 ``hop`` 元信息后再细化。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from memory_app.graph_index import MemoryGraph, entity_node_id
from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.retrieval.channels.base import BaseRetrievalChannel
from memory_app.retrieval.channels.entity import _fallback_tokenize

logger = logging.getLogger(__name__)


class GraphChannel(BaseRetrievalChannel):
    """图遍历召回。"""

    channel_name = "graph"

    def __init__(
        self,
        memory_graph: MemoryGraph | None = None,
        mongo_repo: Any | None = None,
        entity_extractor: Any | None = None,
        *,
        max_depth: int = 2,
    ) -> None:
        self.memory_graph = memory_graph
        self.mongo_repo = mongo_repo
        self.entity_extractor = entity_extractor
        self.max_depth = max(0, min(int(max_depth), 3))

    # ────────────────────────────────────────────────────────────────────────
    # 依赖
    # ────────────────────────────────────────────────────────────────────────
    def _check_dependencies(self) -> None:
        if self.memory_graph is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "memory_graph_unset",
                "GraphChannel: memory_graph not set",
                retryable=True,
            )
        if self.mongo_repo is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "mongo_repo_unset",
                "GraphChannel: mongo_repo not set",
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
        entities = await self._extract_entities(query)
        if not entities:
            return {"hits": [], "entities": []}

        # 并发 BFS:每个实体一次 traverse,gather 把 N×RTT 压成 1×RTT。
        # 单条失败不影响整体(return_exceptions=True 捕获,后续按 ent 顺序合并)。
        seeds = [(ent, entity_node_id(tenant_id, user_id, ent)) for ent in entities]
        traverse_results = await asyncio.gather(
            *[
                self.memory_graph.get_neighbors(user_id, seed, max_depth=self.max_depth)
                for _, seed in seeds
            ],
            return_exceptions=True,
        )
        all_mem_ids: list[str] = []
        seen: set[str] = set()
        for (ent, _seed), r in zip(seeds, traverse_results):
            if isinstance(r, BaseException):
                logger.warning("graph traverse failed for entity %s: %s", ent, r)
                continue
            for m in r:
                if m not in seen:
                    seen.add(m)
                    all_mem_ids.append(m)
        if not all_mem_ids:
            return {"hits": [], "entities": list(entities)}

        # 取回 MemCell —— 单次 $in 替代 N 次 find_one。
        # 关键:over-fetch 一倍候选(再受 ``top_k`` 约束)避免"BFS 命中 ID 但
        # Mongo 中已 archive/删除"时返回不足 top_k 条。
        over_fetch = max(top_k * 2, top_k)
        candidate_ids = all_mem_ids[:over_fetch]
        cells = await self._fetch_cells(candidate_ids)
        # 图与实体 简化:统一给 1.0;RRF 自然吸收尺度
        out: list[tuple[float, Any]] = [(1.0, c) for c in cells[:top_k]]
        return {"hits": out, "entities": list(entities)}

    async def _fetch_cells(self, ids: list[str]) -> list:
        """优先批量取(``get_by_ids``);老版 repo 无该方法时退化到 gather。"""
        if not ids:
            return []
        batch_fn = getattr(self.mongo_repo, "get_by_ids", None)
        if callable(batch_fn):
            return await batch_fn(ids)
        # asyncio 已在模块顶部 import,这里直接用
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
        items = raw.get("hits", [])
        out: list[RankedMemory] = []
        for score, cell in items:
            out.append(
                RankedMemory(
                    memory_id=cell.mem_cell_id,
                    memory_type=MemoryType.EPISODIC,
                    content=cell.text or "",
                    score=float(score),
                    source_channel=self.channel_name,
                    metadata={
                        "matched_entities": list(raw.get("entities", [])),
                        "tenant_id": cell.tenant_id,
                        "user_id": cell.user_id,
                    },
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
            logger.warning("entity_extractor failed (graph fallback): %s", e)
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


__all__ = ["GraphChannel"]
