"""GraphChannel + graph_traversal 插件测试(Step 7.4)。"""

from __future__ import annotations

import pytest

from memory_app.graph_index import InMemoryGraph, MemoryGraph
from memory_app.internal_models import MemCell
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.plugins_default.graph_traversal_channel import GraphTraversalChannel
from memory_app.retrieval.channels.graph import GraphChannel


class _FakeMongoRepo:
    def __init__(self):
        self.store: dict[str, MemCell] = {}

    async def insert(self, cell):
        self.store[cell.mem_cell_id] = cell

    async def get_by_id(self, mid):
        return self.store.get(mid)


def _ctx() -> RetrievalContext:
    return RetrievalContext(tenant_id="t1", user_id="u1")


# ════════════════════════════════════════════════════════════════════════════
# GraphChannel 核心
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestGraphChannel:
    async def _setup(self):
        repo = _FakeMongoRepo()
        await repo.insert(MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="北京天气好", mem_cell_id="mc1",
        ))
        await repo.insert(MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="北京美食多", mem_cell_id="mc2",
        ))
        store = InMemoryGraph()
        graph = MemoryGraph(store)
        await graph.add_memory_node("mc1", ["北京", "天气"], "t1", "u1")
        await graph.add_memory_node("mc2", ["北京", "美食"], "t1", "u1")
        return repo, graph

    async def test_search_via_entity(self):
        repo, graph = await self._setup()
        ch = GraphChannel(memory_graph=graph, mongo_repo=repo)
        hits = await ch.search("t1", "u1", "北京", top_k=10)
        assert {h.memory_id for h in hits} == {"mc1", "mc2"}
        assert all(h.source_channel == "graph" for h in hits)

    async def test_search_no_entity(self):
        repo, graph = await self._setup()
        ch = GraphChannel(memory_graph=graph, mongo_repo=repo)
        # 空查询 → 短路返回空
        hits = await ch.search("t1", "u1", "")
        assert hits == []

    async def test_search_unknown_entity(self):
        repo, graph = await self._setup()
        ch = GraphChannel(memory_graph=graph, mongo_repo=repo)
        hits = await ch.search("t1", "u1", "巴黎", top_k=10)
        assert hits == []

    async def test_unset_graph_raises(self):
        ch = GraphChannel(memory_graph=None, mongo_repo=_FakeMongoRepo())
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY

    async def test_top_k_truncation(self):
        repo, graph = await self._setup()
        ch = GraphChannel(memory_graph=graph, mongo_repo=repo)
        hits = await ch.search("t1", "u1", "北京", top_k=1)
        assert len(hits) == 1


# ════════════════════════════════════════════════════════════════════════════
# graph_traversal 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestGraphTraversalPlugin:
    async def test_unbound_raises(self):
        plugin = GraphTraversalChannel()
        await plugin.start({})
        with pytest.raises(PluginError):
            await plugin.retrieve("北京", _ctx(), 5)

    async def test_bound_returns(self):
        plugin = GraphTraversalChannel()
        await plugin.start({})
        repo = _FakeMongoRepo()
        await repo.insert(MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="北京", mem_cell_id="mc1",
        ))
        store = InMemoryGraph()
        graph = MemoryGraph(store)
        await graph.add_memory_node("mc1", ["北京"], "t1", "u1")
        plugin.bind_memory_graph(graph)
        plugin.bind_mongo_repo(repo)
        hits = await plugin.retrieve("北京", _ctx(), 5)
        assert len(hits) == 1
        assert hits[0].source_channel == "graph"

    async def test_health(self):
        plugin = GraphTraversalChannel()
        await plugin.start({"max_depth": 3})
        h = await plugin.health()
        assert h["status"] == "degraded"
        assert "max_depth=3" in h["detail"]

    async def test_channel_name(self):
        plugin = GraphTraversalChannel()
        await plugin.start({})
        assert plugin.channel_name == "graph"

    async def test_max_depth_clamped(self):
        plugin = GraphTraversalChannel()
        await plugin.start({"max_depth": 10})
        assert plugin._max_depth == 3  # clamp 到 3
