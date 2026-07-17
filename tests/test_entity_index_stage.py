"""EntityIndexStage 单测(Phase 7 Step 7.1 / 7.3)。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- 情景 ``key_entities`` 写入 EntityStore + MemoryGraph
- 多情景去重 / 顺序保留
- 仅 EntityStore / 仅 MemoryGraph / 都为 None 的退化路径
- ``entity_extractor`` 兜底:无情景实体时从 cell.text 抽取
- 写入失败仅 warn,不抛
- 与 ``ColdPathPipeline.extra_stages`` 集成端到端
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory_app.entity_store import InMemoryEntityStore
from memory_app.graph_index import (
    InMemoryGraph,
    MemoryGraph,
    entity_node_id,
    memory_node_id,
)
from memory_app.internal_models import EpisodicMemory, MemCell
from memory_app.pipelines import (
    ColdPathContext,
    ColdPathPipeline,
    EntityIndexStage,
)


# ════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════
def _cell(
    text: str = "我下周要去北京出差,顺便见一下张三", mem_cell_id: str = "mc1"
) -> MemCell:
    return MemCell(
        mem_cell_id=mem_cell_id,
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        text=text,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _episode(
    *,
    mem_cell_id: str = "mc1",
    summary: str = "出差",
    key_entities: list[str] | None = None,
) -> EpisodicMemory:
    return EpisodicMemory(
        episode_id=f"ep-{mem_cell_id}",
        mem_cell_id=mem_cell_id,
        tenant_id="t1",
        user_id="u1",
        summary=summary,
        key_entities=key_entities or [],
    )


class _FakeEntityExtractor:
    """模拟一个返回字符串列表的简单 extractor。"""

    def __init__(self, entities: list[str], fail: bool = False) -> None:
        self._entities = entities
        self.fail = fail
        self.calls = 0

    async def extract(self, text: str):
        self.calls += 1
        if self.fail:
            raise RuntimeError("extractor exploded")
        return list(self._entities)


# ════════════════════════════════════════════════════════════════════════════
# EntityIndexStage 行为
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestEntityIndexStage:
    async def test_writes_entities_to_store_and_graph(self):
        store = InMemoryEntityStore()
        graph = MemoryGraph(InMemoryGraph())
        stage = EntityIndexStage(entity_store=store, memory_graph=graph)

        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=["北京", "出差", "张三"])]
        ctx = await stage.run(ctx)

        # EntityStore
        ids = await store.find_by_entities(["北京"], "t1", "u1")
        assert "mc1" in ids
        ids = await store.find_by_entities(["张三"], "t1", "u1")
        assert "mc1" in ids
        # MemoryGraph: memory 节点 + entity 节点 + MENTIONS 边
        related = await graph.find_related_memories("t1", "u1", "出差")
        assert "mc1" in related
        # metrics
        assert ctx.metrics["entity_index_count"] == 3
        assert ctx.metrics["graph_entity_count"] == 3
        assert ctx.metrics["graph_edge_count"] == 3

    async def test_dedupes_across_multiple_episodes(self):
        store = InMemoryEntityStore()
        stage = EntityIndexStage(entity_store=store)
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [
            _episode(key_entities=["北京", "出差"]),
            _episode(key_entities=["北京", "张三"]),
        ]
        ctx = await stage.run(ctx)
        # 北京 + 出差 + 张三 = 3 个去重实体
        assert ctx.metrics["entity_index_count"] == 3

    async def test_strips_blank_entities(self):
        store = InMemoryEntityStore()
        stage = EntityIndexStage(entity_store=store)
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=["  ", "", "北京", "  上海  "])]
        ctx = await stage.run(ctx)
        assert ctx.metrics["entity_index_count"] == 2
        ids = await store.find_by_entities(["上海"], "t1", "u1")
        assert "mc1" in ids

    async def test_unbound_components_warn(self):
        stage = EntityIndexStage(entity_store=None, memory_graph=None)
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=["北京"])]
        ctx = await stage.run(ctx)
        assert "entity_index_unbound" in ctx.warnings
        # 不抛异常
        assert "entity_index_count" not in ctx.metrics

    async def test_only_entity_store_bound(self):
        store = InMemoryEntityStore()
        stage = EntityIndexStage(entity_store=store, memory_graph=None)
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=["北京"])]
        ctx = await stage.run(ctx)
        ids = await store.find_by_entities(["北京"], "t1", "u1")
        assert "mc1" in ids
        # graph 未绑 → 无 graph metrics
        assert "graph_entity_count" not in ctx.metrics

    async def test_only_graph_bound(self):
        graph = MemoryGraph(InMemoryGraph())
        stage = EntityIndexStage(entity_store=None, memory_graph=graph)
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=["北京"])]
        ctx = await stage.run(ctx)
        related = await graph.find_related_memories("t1", "u1", "北京")
        assert "mc1" in related
        assert ctx.metrics["graph_entity_count"] == 1

    async def test_empty_episodes_skips(self):
        store = InMemoryEntityStore()
        stage = EntityIndexStage(entity_store=store)
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = []
        ctx = await stage.run(ctx)
        assert ctx.metrics["entity_index_count"] == 0
        # 没写任何东西
        ids = await store.find_by_entities(["北京"], "t1", "u1")
        assert ids == []

    async def test_extractor_fallback_when_no_episode_entities(self):
        store = InMemoryEntityStore()
        extractor = _FakeEntityExtractor(["上海", "南京"])
        stage = EntityIndexStage(
            entity_store=store, entity_extractor=extractor
        )
        ctx = ColdPathContext(cell=_cell(text="上海与南京之间的高铁很快"))
        ctx.episodes = [_episode(key_entities=[])]  # 无实体
        ctx = await stage.run(ctx)

        assert extractor.calls == 1
        assert ctx.metrics["entity_index_count"] == 2
        ids = await store.find_by_entities(["上海"], "t1", "u1")
        assert "mc1" in ids

    async def test_extractor_failure_emits_warning(self):
        store = InMemoryEntityStore()
        extractor = _FakeEntityExtractor([], fail=True)
        stage = EntityIndexStage(
            entity_store=store, entity_extractor=extractor
        )
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=[])]
        ctx = await stage.run(ctx)
        assert any(w.startswith("entity_index_extractor_failed") for w in ctx.warnings)

    async def test_store_failure_isolated_from_graph(self):
        class _BoomStore:
            async def upsert_entities(self, *a, **kw):
                raise RuntimeError("store down")

        graph = MemoryGraph(InMemoryGraph())
        stage = EntityIndexStage(entity_store=_BoomStore(), memory_graph=graph)
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=["北京"])]
        ctx = await stage.run(ctx)
        assert any(
            w.startswith("entity_store_upsert_failed") for w in ctx.warnings
        )
        # graph 仍写入成功
        related = await graph.find_related_memories("t1", "u1", "北京")
        assert "mc1" in related

    async def test_graph_failure_isolated_from_store(self):
        class _BoomGraph:
            async def add_memory_node(self, *a, **kw):
                raise RuntimeError("graph down")

        store = InMemoryEntityStore()
        stage = EntityIndexStage(entity_store=store, memory_graph=_BoomGraph())
        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=["北京"])]
        ctx = await stage.run(ctx)
        assert any(w.startswith("memory_graph_failed") for w in ctx.warnings)
        ids = await store.find_by_entities(["北京"], "t1", "u1")
        assert "mc1" in ids

    async def test_bind_setters_rebind_dependencies(self):
        stage = EntityIndexStage()
        store = InMemoryEntityStore()
        graph = MemoryGraph(InMemoryGraph())
        extractor = _FakeEntityExtractor(["X"])
        stage.bind_entity_store(store)
        stage.bind_memory_graph(graph)
        stage.bind_entity_extractor(extractor)

        ctx = ColdPathContext(cell=_cell())
        ctx.episodes = [_episode(key_entities=["北京"])]
        ctx = await stage.run(ctx)
        assert ctx.metrics["entity_index_count"] == 1
        assert ctx.metrics["graph_entity_count"] == 1


# ════════════════════════════════════════════════════════════════════════════
# 端到端:与 ColdPathPipeline.extra_stages 集成
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestEntityIndexStagePipelineIntegration:
    async def test_full_cold_path_with_entity_index(self):
        from memory_app.internal_models import KnowledgeType, SemanticMemory
        from memory_app.plugins.spi.clusterer import ClusterAssignmentMeta

        class _FakeEpisode:
            async def extract(self, memcell, old_memories=None, scenario=None):
                return [
                    EpisodicMemory(
                        episode_id=f"ep-{memcell.mem_cell_id}",
                        mem_cell_id=memcell.mem_cell_id,
                        tenant_id=memcell.tenant_id,
                        user_id=memcell.user_id,
                        summary="出差北京",
                        key_entities=["北京", "出差"],
                    )
                ]

        class _FakeSemantic:
            async def extract_for_episode(self, episode):
                return [
                    SemanticMemory(
                        tenant_id=episode.tenant_id,
                        user_id=episode.user_id,
                        content="X 在 Y 出差",
                        knowledge_type=KnowledgeType.FACT,
                        source_episode_ids=[episode.episode_id],
                    )
                ]

        class _FakeClusterer:
            async def cluster(self, group_id, memcell):
                return f"sc-{memcell.mem_cell_id}", ClusterAssignmentMeta(
                    similarity=0.9, is_new_cluster=False
                )

        store = InMemoryEntityStore()
        graph = MemoryGraph(InMemoryGraph())
        index_stage = EntityIndexStage(entity_store=store, memory_graph=graph)
        pipe = ColdPathPipeline(
            episode_extractor=_FakeEpisode(),
            semantic_extractor=_FakeSemantic(),
            clusterer=_FakeClusterer(),
            extra_stages=[index_stage],
        )
        ctx = await pipe.execute(_cell())

        assert ctx.metrics["entity_index_count"] == 2
        assert ctx.metrics["graph_entity_count"] == 2
        # graph 节点确实写了
        node = graph.store.get_node("u1", entity_node_id("t1", "u1", "北京"))
        assert node is not None
        node = graph.store.get_node("u1", memory_node_id("mc1"))
        assert node is not None
