"""MemoryGraph + InMemoryGraph + InMemoryLRUGraphStore 测试(Step 7.3)。"""

from __future__ import annotations

import pytest

from memory_app.graph_index import (
    GraphEdgeRecord,
    GraphNodeRecord,
    InMemoryGraph,
    MemoryGraph,
    edge_id,
    entity_node_id,
    memory_node_id,
)
from memory_app.plugins.spi.graph_store import GraphEdge, GraphNode
from memory_app.plugins_default.in_memory_lru_graph import (
    InMemoryLRUGraphStore,
)


# ════════════════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════════════════
class TestNodeIdHelpers:
    def test_entity_node_id(self):
        assert entity_node_id("t1", "u1", "北京") == "entity:t1:u1:北京"

    def test_memory_node_id(self):
        assert memory_node_id("mc1") == "memory:mc1"

    def test_edge_id(self):
        assert edge_id("a", "MENTIONS", "b") == "a--MENTIONS-->b"


# ════════════════════════════════════════════════════════════════════════════
# InMemoryGraph
# ════════════════════════════════════════════════════════════════════════════
class TestInMemoryGraph:
    def test_upsert_and_get_node(self):
        g = InMemoryGraph()
        n = GraphNodeRecord(node_id="x", node_type="entity", user_id="u1", label="x")
        g.upsert_node(n)
        got = g.get_node("u1", "x")
        assert got is not None
        assert got.label == "x"

    def test_upsert_edge(self):
        g = InMemoryGraph()
        g.upsert_node(GraphNodeRecord(node_id="m1", node_type="memory", user_id="u1"))
        g.upsert_node(GraphNodeRecord(node_id="e1", node_type="entity", user_id="u1"))
        g.upsert_edge(
            GraphEdgeRecord(
                edge_id="m1--MENTIONS-->e1",
                edge_type="MENTIONS",
                source_node_id="m1",
                target_node_id="e1",
                user_id="u1",
            )
        )
        # 邻接索引就绪
        assert "m1--MENTIONS-->e1" in g._adj["u1"]["m1"]
        assert "m1--MENTIONS-->e1" in g._adj["u1"]["e1"]

    def test_upsert_edge_idempotent_strength(self):
        g = InMemoryGraph()
        g.upsert_node(GraphNodeRecord(node_id="m1", node_type="memory", user_id="u1"))
        g.upsert_node(GraphNodeRecord(node_id="e1", node_type="entity", user_id="u1"))
        e = GraphEdgeRecord(
            edge_id="m1--MENTIONS-->e1",
            edge_type="MENTIONS",
            source_node_id="m1",
            target_node_id="e1",
            user_id="u1",
            source_memory_ids=["mc1"],
        )
        g.upsert_edge(e)
        # 第二次同 edge_id → strength 累加
        e2 = GraphEdgeRecord(
            edge_id="m1--MENTIONS-->e1",
            edge_type="MENTIONS",
            source_node_id="m1",
            target_node_id="e1",
            user_id="u1",
            source_memory_ids=["mc2"],
        )
        g.upsert_edge(e2)
        edge = g._edges["u1"]["m1--MENTIONS-->e1"]
        assert edge.strength == 2.0
        assert "mc1" in edge.source_memory_ids and "mc2" in edge.source_memory_ids

    def test_traverse_2_hops(self):
        g = InMemoryGraph()
        # m1 - e1 - m2  (2 跳)
        g.upsert_node(GraphNodeRecord(node_id="m1", node_type="memory", user_id="u1", memory_id="mc1"))
        g.upsert_node(GraphNodeRecord(node_id="m2", node_type="memory", user_id="u1", memory_id="mc2"))
        g.upsert_node(GraphNodeRecord(node_id="e1", node_type="entity", user_id="u1"))
        g.upsert_edge(GraphEdgeRecord(
            edge_id="m1--MENTIONS-->e1", edge_type="MENTIONS",
            source_node_id="m1", target_node_id="e1", user_id="u1",
        ))
        g.upsert_edge(GraphEdgeRecord(
            edge_id="m2--MENTIONS-->e1", edge_type="MENTIONS",
            source_node_id="m2", target_node_id="e1", user_id="u1",
        ))
        # 从 e1 出发,2 跳能到 m1 + m2
        nodes, edges = g.traverse("u1", ["e1"], max_hops=2)
        ids = {n.node_id for n in nodes}
        assert "m1" in ids and "m2" in ids
        assert len(edges) == 2

    def test_traverse_filters_invalid(self):
        g = InMemoryGraph()
        g.upsert_node(GraphNodeRecord(node_id="m1", node_type="memory", user_id="u1"))
        g.upsert_node(GraphNodeRecord(node_id="e1", node_type="entity", user_id="u1"))
        g.upsert_edge(GraphEdgeRecord(
            edge_id="m1--MENTIONS-->e1", edge_type="MENTIONS",
            source_node_id="m1", target_node_id="e1", user_id="u1",
            is_valid=False,
        ))
        nodes, edges = g.traverse("u1", ["e1"], max_hops=2)
        # is_valid=false 的边应被忽略
        assert edges == []

    def test_traverse_edge_type_filter(self):
        g = InMemoryGraph()
        g.upsert_node(GraphNodeRecord(node_id="m1", node_type="memory", user_id="u1"))
        g.upsert_node(GraphNodeRecord(node_id="e1", node_type="entity", user_id="u1"))
        g.upsert_edge(GraphEdgeRecord(
            edge_id="m1--MENTIONS-->e1", edge_type="MENTIONS",
            source_node_id="m1", target_node_id="e1", user_id="u1",
        ))
        g.upsert_edge(GraphEdgeRecord(
            edge_id="m1--SUPPORTS-->e1", edge_type="SUPPORTS",
            source_node_id="m1", target_node_id="e1", user_id="u1",
        ))
        # 只筛选 SUPPORTS
        _nodes, edges = g.traverse("u1", ["m1"], max_hops=1, edge_types=["SUPPORTS"])
        assert all(e.edge_type == "SUPPORTS" for e in edges)
        assert len(edges) == 1

    def test_user_isolation(self):
        g = InMemoryGraph()
        g.upsert_node(GraphNodeRecord(node_id="x", node_type="entity", user_id="u1"))
        g.upsert_node(GraphNodeRecord(node_id="x", node_type="entity", user_id="u2"))
        # 不同 user 各自独立
        assert g.get_node("u1", "x") is not None
        assert g.get_node("u2", "x") is not None

    def test_lru_eviction(self):
        g = InMemoryGraph(max_nodes_per_user=3, max_edges_per_user=10)
        for i in range(5):
            g.upsert_node(GraphNodeRecord(
                node_id=f"n{i}", node_type="entity", user_id="u1"
            ))
        st = g.stats()
        assert st["total_nodes"] == 3  # LRU 限到 3

    def test_max_hops_clamp(self):
        g = InMemoryGraph()
        g.upsert_node(GraphNodeRecord(node_id="x", node_type="entity", user_id="u1"))
        # max_hops > 3 应被硬限制到 3
        nodes, _ = g.traverse("u1", ["x"], max_hops=99)
        assert len(nodes) == 1


# ════════════════════════════════════════════════════════════════════════════
# MemoryGraph 业务门面
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestMemoryGraph:
    async def test_add_memory_node_with_entities(self):
        store = InMemoryGraph()
        graph = MemoryGraph(store)
        out = await graph.add_memory_node(
            "mc1", ["北京", "出差"], "t1", "u1"
        )
        assert out["entity_count"] == 2
        assert out["edge_count"] == 2
        # memory + 2 个 entity 节点
        assert store.stats()["total_nodes"] == 3
        assert store.stats()["total_edges"] == 2

    async def test_get_neighbors_via_entity(self):
        store = InMemoryGraph()
        graph = MemoryGraph(store)
        await graph.add_memory_node("mc1", ["北京"], "t1", "u1")
        await graph.add_memory_node("mc2", ["北京"], "t1", "u1")
        # 从实体 "北京" 出发 → 应能找回 mc1 + mc2
        ids = await graph.find_related_memories("t1", "u1", "北京", max_depth=2)
        assert set(ids) == {"mc1", "mc2"}

    async def test_get_neighbors_empty(self):
        store = InMemoryGraph()
        graph = MemoryGraph(store)
        ids = await graph.get_neighbors("u1", "nonexistent")
        assert ids == []

    async def test_user_isolation(self):
        store = InMemoryGraph()
        graph = MemoryGraph(store)
        await graph.add_memory_node("mc1", ["北京"], "t1", "u1")
        await graph.add_memory_node("mc2", ["北京"], "t1", "u2")
        u1_ids = await graph.find_related_memories("t1", "u1", "北京")
        assert u1_ids == ["mc1"]

    async def test_dedupe_entities(self):
        store = InMemoryGraph()
        graph = MemoryGraph(store)
        out = await graph.add_memory_node("mc1", ["北京", "北京", " "], "t1", "u1")
        assert out["entity_count"] == 1


# ════════════════════════════════════════════════════════════════════════════
# InMemoryLRUGraphStore 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestInMemoryLRUGraphStorePlugin:
    async def test_spi_round_trip(self):
        plugin = InMemoryLRUGraphStore()
        await plugin.start({})
        await plugin.add_node(GraphNode(
            id="x", node_type="entity", label="x", user_id="u1",
        ))
        got = await plugin.get_node("u1", "x")
        assert got is not None and got.label == "x"

    async def test_spi_traverse(self):
        plugin = InMemoryLRUGraphStore()
        await plugin.start({})
        await plugin.add_node(GraphNode(
            id="m1", node_type="memory", label="m1", user_id="u1",
        ))
        await plugin.add_node(GraphNode(
            id="e1", node_type="entity", label="e1", user_id="u1",
        ))
        await plugin.add_edge(GraphEdge(
            id="m1--MENTIONS-->e1",
            edge_type="MENTIONS",
            source_node_id="m1",
            target_node_id="e1",
            extra_json={"user_id": "u1"},
        ))
        nodes, edges = await plugin.traverse("u1", ["e1"], max_hops=1)
        ids = {n.id for n in nodes}
        assert "m1" in ids
        assert len(edges) == 1

    async def test_health(self):
        plugin = InMemoryLRUGraphStore()
        await plugin.start({})
        h = await plugin.health()
        assert h["status"] == "ok"

    async def test_metrics(self):
        plugin = InMemoryLRUGraphStore()
        await plugin.start({})
        await plugin.add_node(GraphNode(
            id="x", node_type="entity", label="x", user_id="u1",
        ))
        m = await plugin.metrics()
        assert m["graph_total_nodes"] == 1

    async def test_works_with_memory_graph(self):
        """插件作为 MemoryGraph.store 的鸭子类型工作。"""
        plugin = InMemoryLRUGraphStore()
        await plugin.start({})
        graph = MemoryGraph(plugin)
        await graph.add_memory_node("mc1", ["北京"], "t1", "u1")
        ids = await graph.find_related_memories("t1", "u1", "北京")
        assert "mc1" in ids

    async def test_update_edge(self):
        plugin = InMemoryLRUGraphStore()
        await plugin.start({})
        await plugin.add_node(GraphNode(
            id="m1", node_type="memory", label="m1", user_id="u1",
        ))
        await plugin.add_node(GraphNode(
            id="e1", node_type="entity", label="e1", user_id="u1",
        ))
        await plugin.add_edge(GraphEdge(
            id="m1--MENTIONS-->e1",
            edge_type="MENTIONS",
            source_node_id="m1",
            target_node_id="e1",
            extra_json={"user_id": "u1"},
        ))
        ok = await plugin.update_edge(
            "u1", "m1--MENTIONS-->e1", {"is_valid": False}
        )
        assert ok is True
        nodes, edges = await plugin.traverse("u1", ["e1"], max_hops=1)
        assert edges == []  # is_valid=false 后被过滤
