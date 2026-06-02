"""ColdPathPipeline —— 写入异步冷路径。

═══════════════════════════════════════════════════════════════════════════════
阶段顺序
═══════════════════════════════════════════════════════════════════════════════
::

    EpisodeExtractStage   (LLM 情景抽取,)
        ↓
    SemanticExtractStage  (LLM 语义联想,)
        ↓
    ClusterStage          (MemScene 增量聚类,)
        ↓
    EntityIndexStage      (实体倒排 + 图节点构建, / 7.3;extra_stage 注入)

═══════════════════════════════════════════════════════════════════════════════
失败语义
═══════════════════════════════════════════════════════════════════════════════
- ``EpisodeExtractStage`` 失败 → 抛上层(BackgroundTaskRunner 走重试 / DLQ)
- ``SemanticExtractStage`` / ``ClusterStage`` / ``EntityIndexStage`` 失败 →
  ctx 记录 ``warn``,管线继续(增强能力,失败不影响情景沉淀)

═══════════════════════════════════════════════════════════════════════════════
组件注入
═══════════════════════════════════════════════════════════════════════════════
- ``episode_extractor``  实现 ``await extract(MemCell, scenario) -> list[EpisodicMemory]``
- ``semantic_extractor`` 实现 ``await extract_for_episode(EpisodicMemory) -> list[SemanticMemory]``
- ``clusterer``          实现 ``await cluster(group_id, MemCell) -> (cluster_id, meta)``
- ``entity_store``       :class:`EntityStore`(图与实体);None → 跳过倒排写
- ``memory_graph``       :class:`MemoryGraph`(图与实体);None → 跳过图节点写

任意为 None → 对应阶段跳过(便于灰度 / 渐进上线)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from memory_app.internal_models import (
    EpisodicMemory,
    MemCell,
    SemanticMemory,
)
from memory_app.pipelines.base import BasePipeline, PipelineStage
from memory_app.plugins.spi.episode_extractor import ScenarioType

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 鸭子类型协议
# ════════════════════════════════════════════════════════════════════════════
class _EpisodeExtractorProto(Protocol):
    async def extract(
        self,
        memcell: MemCell,
        old_memories: list[SemanticMemory] | None = None,
        scenario: ScenarioType = ScenarioType.GROUP_CHAT,
    ) -> list[EpisodicMemory]: ...


class _SemanticExtractorProto(Protocol):
    async def extract_for_episode(self, episode: EpisodicMemory) -> list[SemanticMemory]: ...


class _ClustererProto(Protocol):
    async def cluster(
        self, group_id: str, memcell: MemCell
    ) -> tuple[str, Any]: ...


# ════════════════════════════════════════════════════════════════════════════
# 上下文
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ColdPathContext:
    """ColdPathPipeline 阶段间共享上下文。"""

    cell: MemCell
    scenario: ScenarioType = ScenarioType.GROUP_CHAT

    episodes: list[EpisodicMemory] = field(default_factory=list)
    semantics: list[SemanticMemory] = field(default_factory=list)
    cluster_id: str | None = None
    cluster_meta: Any = None

    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════════
# Stage 1: 情景抽取
# ════════════════════════════════════════════════════════════════════════════
class EpisodeExtractStage(PipelineStage[ColdPathContext]):
    name = "episode_extract"

    def __init__(self, extractor: _EpisodeExtractorProto | None) -> None:
        self._extractor = extractor

    async def run(self, ctx: ColdPathContext) -> ColdPathContext:
        if self._extractor is None:
            ctx.warnings.append("episode_extractor_unbound")
            return ctx
        ctx.episodes = await self._extractor.extract(ctx.cell, scenario=ctx.scenario)
        ctx.metrics["episode_count"] = len(ctx.episodes)
        logger.debug(
            "cold_path episodes: cell=%s → %d episodes",
            ctx.cell.mem_cell_id, len(ctx.episodes),
        )
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Stage 2: 语义抽取
# ════════════════════════════════════════════════════════════════════════════
class SemanticExtractStage(PipelineStage[ColdPathContext]):
    name = "semantic_extract"

    def __init__(
        self,
        extractor: _SemanticExtractorProto | None,
        *,
        llm_max_concurrent: int = 8,
    ) -> None:
        self._extractor = extractor
        self._llm_max_concurrent = max(1, int(llm_max_concurrent))

    async def run(self, ctx: ColdPathContext) -> ColdPathContext:
        if self._extractor is None:
            ctx.warnings.append("semantic_extractor_unbound")
            return ctx
        if not ctx.episodes:
            ctx.warnings.append("no_episodes_for_semantic")
            return ctx
        import asyncio

        from memory_app.concurrency import gather_with_limit

        results = await gather_with_limit(
            (self._extractor.extract_for_episode(ep) for ep in ctx.episodes),
            self._llm_max_concurrent,
            return_exceptions=True,
        )
        all_semantics: list[SemanticMemory] = []
        for ep, r in zip(ctx.episodes, results):
            # CancelledError 必须冒泡,否则后台任务的取消请求被吞,Stage 4/5 会继续跑
            if isinstance(r, asyncio.CancelledError):
                raise r
            if isinstance(r, BaseException):
                logger.warning(
                    "semantic extract failed for episode=%s: %s", ep.episode_id, r
                )
                ctx.warnings.append(f"semantic_extract_failed:{ep.episode_id}")
                continue
            all_semantics.extend(r)
        ctx.semantics = all_semantics
        ctx.metrics["semantic_count"] = len(all_semantics)
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Stage 3: 聚类
# ════════════════════════════════════════════════════════════════════════════
class ClusterStage(PipelineStage[ColdPathContext]):
    name = "cluster"

    def __init__(self, clusterer: _ClustererProto | None) -> None:
        self._clusterer = clusterer

    async def run(self, ctx: ColdPathContext) -> ColdPathContext:
        if self._clusterer is None:
            ctx.warnings.append("clusterer_unbound")
            return ctx
        group_id = (
            ctx.cell.group_id or ctx.cell.session_id or f"u:{ctx.cell.user_id}"
        )
        try:
            cluster_id, meta = await self._clusterer.cluster(group_id, ctx.cell)
        except Exception as e:  # noqa: BLE001
            # 聚类失败不影响情景 / 语义已落库;ctx 记 warn
            logger.warning("cluster failed for cell=%s: %s", ctx.cell.mem_cell_id, e)
            ctx.warnings.append(f"cluster_failed:{e.__class__.__name__}")
            return ctx
        ctx.cluster_id = cluster_id
        ctx.cluster_meta = meta
        ctx.metrics["cluster_id"] = cluster_id
        ctx.metrics["cluster_is_new"] = bool(getattr(meta, "is_new_cluster", False))
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Stage 4: 实体索引( / 7.3)
# ════════════════════════════════════════════════════════════════════════════
class EntityIndexStage(PipelineStage[ColdPathContext]):
    """把 ``ctx.episodes[*].key_entities`` 索引到 EntityStore + MemoryGraph。

    图与实体 集成点:
    - 写 EntityStore(倒排索引,供 EntityChannel 召回)
    - 写 MemoryGraph(memory→entity ``MENTIONS`` 边,供 GraphChannel 遍历)

    依赖松耦合:``entity_store`` / ``memory_graph`` 可独立 None,任一 None →
    跳过对应写入;两者都 None → 整阶段空跑(只 ``warn``,不抛)。
    """

    name = "entity_index"

    def __init__(
        self,
        entity_store: Any | None = None,
        memory_graph: Any | None = None,
        entity_extractor: Any | None = None,
    ) -> None:
        self._entity_store = entity_store
        self._memory_graph = memory_graph
        self._entity_extractor = entity_extractor

    # 便利:deps.py 在 图与实体 装配后再绑定
    def bind_entity_store(self, store: Any) -> None:
        self._entity_store = store

    def bind_memory_graph(self, graph: Any) -> None:
        self._memory_graph = graph

    def bind_entity_extractor(self, extractor: Any) -> None:
        self._entity_extractor = extractor

    async def run(self, ctx: ColdPathContext) -> ColdPathContext:
        if self._entity_store is None and self._memory_graph is None:
            ctx.warnings.append("entity_index_unbound")
            return ctx

        entities = self._collect_episode_entities(ctx)
        # 兜底:无情景或情景未填 key_entities 时,从 cell.text 抽实体
        if not entities and self._entity_extractor is not None:
            try:
                entities = await self._extract_from_text(ctx.cell.text or "")
            except Exception as e:  # noqa: BLE001
                logger.warning("entity_index extractor fallback failed: %s", e)
                ctx.warnings.append(
                    f"entity_index_extractor_failed:{e.__class__.__name__}"
                )

        if not entities:
            ctx.metrics["entity_index_count"] = 0
            return ctx

        cell = ctx.cell
        upserted = 0
        if self._entity_store is not None:
            try:
                upserted = await self._entity_store.upsert_entities(
                    cell.mem_cell_id, entities, cell.tenant_id, cell.user_id
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "entity_index store upsert failed (cell=%s): %s",
                    cell.mem_cell_id, e,
                )
                ctx.warnings.append(
                    f"entity_store_upsert_failed:{e.__class__.__name__}"
                )
        ctx.metrics["entity_index_count"] = int(upserted)

        if self._memory_graph is not None:
            try:
                graph_result = await self._memory_graph.add_memory_node(
                    cell.mem_cell_id, entities, cell.tenant_id, cell.user_id
                )
                ctx.metrics["graph_entity_count"] = int(
                    (graph_result or {}).get("entity_count", 0)
                )
                ctx.metrics["graph_edge_count"] = int(
                    (graph_result or {}).get("edge_count", 0)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "entity_index graph add_memory_node failed (cell=%s): %s",
                    cell.mem_cell_id, e,
                )
                ctx.warnings.append(
                    f"memory_graph_failed:{e.__class__.__name__}"
                )
        return ctx

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _collect_episode_entities(ctx: ColdPathContext) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for ep in ctx.episodes:
            for raw in getattr(ep, "key_entities", None) or []:
                e = (raw or "").strip()
                if not e or e in seen:
                    continue
                seen.add(e)
                out.append(e)
        return out

    async def _extract_from_text(self, text: str) -> list[str]:
        if not text:
            return []
        extracted = await self._entity_extractor.extract(text)
        seen: set[str] = set()
        out: list[str] = []
        for raw in extracted or []:
            e = (getattr(raw, "text", None) or str(raw)).strip()
            key = e.lower()
            if e and key not in seen:
                seen.add(key)
                out.append(e)
        return out


# ════════════════════════════════════════════════════════════════════════════
# 主管线
# ════════════════════════════════════════════════════════════════════════════
class ColdPathPipeline(BasePipeline[MemCell, ColdPathContext, ColdPathContext]):
    """异步冷路径主管线。

    ``execute(cell) -> ColdPathContext``:返回最终 ctx,调用方可读 episodes /
    semantics / cluster_id 等做持久化(冷路径 service 委托)。
    """

    def __init__(
        self,
        *,
        episode_extractor: _EpisodeExtractorProto | None = None,
        semantic_extractor: _SemanticExtractorProto | None = None,
        clusterer: _ClustererProto | None = None,
        scenario: ScenarioType = ScenarioType.GROUP_CHAT,
        extra_stages: list[PipelineStage[ColdPathContext]] | None = None,
        llm_max_concurrent: int = 8,
    ) -> None:
        self._episode_stage = EpisodeExtractStage(episode_extractor)
        self._semantic_stage = SemanticExtractStage(
            semantic_extractor, llm_max_concurrent=llm_max_concurrent
        )
        self._cluster_stage = ClusterStage(clusterer)
        self._scenario = scenario
        self._extra_stages: list[PipelineStage[ColdPathContext]] = list(
            extra_stages or []
        )

    def stages(self) -> list[PipelineStage[ColdPathContext]]:
        return [
            self._episode_stage,
            self._semantic_stage,
            self._cluster_stage,
            *self._extra_stages,
        ]

    async def build_context(self, input_data: MemCell) -> ColdPathContext:
        return ColdPathContext(cell=input_data, scenario=self._scenario)

    async def finalize(self, ctx: ColdPathContext) -> ColdPathContext:
        return ctx

    def add_extra_stage(self, stage: PipelineStage[ColdPathContext]) -> None:
        """对外开放的"追加 extra stage"API,替代 builder 直 mutate ``self._extra_stages``。

        图与实体 GraphComponentsBuilder 用本方法把 EntityIndexStage 挂在主链尾部。
        通过公共 API 而非读私有列表,内部布局变更(如未来改用 immutable tuple)
        不会让 builder 静默 no-op。
        """
        self._extra_stages.append(stage)

    def find_extra_stage(self, predicate) -> "PipelineStage[ColdPathContext] | None":  # type: ignore[no-untyped-def]
        """按谓词查找已注册的 extra stage —— 装配幂等需要(已存在则 rebind 而非新建)。"""
        for s in self._extra_stages:
            if predicate(s):
                return s
        return None


__all__ = [
    "ColdPathPipeline",
    "ColdPathContext",
    "EpisodeExtractStage",
    "SemanticExtractStage",
    "ClusterStage",
    "EntityIndexStage",
]
