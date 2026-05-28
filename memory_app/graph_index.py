"""MemoryGraph —— 记忆图核心算法。

═══════════════════════════════════════════════════════════════════════════════
节点 / 边模型
═══════════════════════════════════════════════════════════════════════════════
节点类型:
- ``memory``     记忆节点(对应 MemCell / EpisodicMemory / SemanticMemory)
- ``entity``     实体节点(``id = entity:<tenant>:<user>:<entity>``)
- ``community``  簇 / 主题节点（REM 巩固阶段生成）
- ``user``       用户节点（预留，当前暂不连）

边类型(对齐 ``GraphStore`` SPI):
- ``MENTIONS``    memory 指向 entity（当前主要写入的边类型）
- ``RELATED_TO``  entity ↔ entity 共现
- ``SUPPORTS`` / ``CONFLICTS`` / ``UPDATES``  巩固阶段写入

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
本模块提供"纯算法 / 业务态"的 MemoryGraph 主类(不直接依赖 SPI)。
插件层 :class:`memory_app.plugins_default.in_memory_lru_graph.InMemoryLRUGraphStore`
是它的 SPI 适配,业务平面经 ``factory.build("memory.storage.graph_store")``
取实例。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 数据模型(轻量 dataclass,与 SPI GraphNode/Edge 互转)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class GraphNodeRecord:
    """节点记录。"""

    node_id: str
    node_type: str  # memory / entity / community / user
    user_id: str
    label: str = ""
    memory_id: str | None = None
    is_valid: bool = True
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdgeRecord:
    """边记录。"""

    edge_id: str
    edge_type: str  # MENTIONS / RELATED_TO / SUPPORTS / CONFLICTS / UPDATES / BELONGS_TO
    source_node_id: str
    target_node_id: str
    user_id: str
    confidence: float = 1.0
    strength: float = 1.0
    source_memory_ids: list[str] = field(default_factory=list)
    is_valid: bool = True
    properties: dict[str, Any] = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def entity_node_id(tenant_id: str, user_id: str, entity: str) -> str:
    return f"entity:{tenant_id}:{user_id}:{entity}"


def memory_node_id(mem_cell_id: str) -> str:
    return f"memory:{mem_cell_id}"


def edge_id(source: str, relation: str, target: str) -> str:
    return f"{source}--{relation}-->{target}"


# ════════════════════════════════════════════════════════════════════════════
# 内存图(供测试 / 评测 / 图与实体 默认)
# ════════════════════════════════════════════════════════════════════════════
class InMemoryGraph:
    """按 ``user_id`` 分片 + LRU 容量上限的内存图。

    数据结构:
    - ``_nodes[user_id][node_id] = GraphNodeRecord``       OrderedDict(LRU 顺序)
    - ``_edges[user_id][edge_id] = GraphEdgeRecord``       OrderedDict(LRU 顺序)
    - ``_adj[user_id][node_id] = set[edge_id]``            邻接索引,加速 traverse

    LRU:超过 ``max_nodes_per_user`` / ``max_edges_per_user`` 时弹出最早项。
    """

    def __init__(
        self,
        *,
        max_nodes_per_user: int = 4096,
        max_edges_per_user: int = 8192,
    ) -> None:
        # 至少 1;生产配置 schema 的 minimum 兜住更合理下限,测试可用小值
        self.max_nodes_per_user = max(1, int(max_nodes_per_user))
        self.max_edges_per_user = max(1, int(max_edges_per_user))
        self._nodes: dict[str, "OrderedDict[str, GraphNodeRecord]"] = defaultdict(OrderedDict)
        self._edges: dict[str, "OrderedDict[str, GraphEdgeRecord]"] = defaultdict(OrderedDict)
        self._adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        # per-user 互斥锁,保护 nodes/edges/adj 三个字典的串行修改 ——
        # 否则并发 upsert_edge(merge: existed.strength += 1, source_memory_ids.append)
        # 与 traverse(读 _adj / _edges 迭代)会互踩:
        #   1) merge 路径 RMW 丢更新(同 edge 并发计 +1 只算一次)
        #   2) 读侧 RuntimeError: dictionary changed size during iteration
        # 用 threading.Lock 而非 asyncio.Lock —— InMemoryGraph 的方法是同步的,
        # 调用方(InMemoryLRUGraphStore async wrapper / MemoryGraph 业务门面)
        # 已在 async 上下文里串行调用,但 traverse 与 upsert 可能跨 await 边界交错。
        self._user_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    # ────────────────────────────────────────────────────────────────────────
    # 节点
    # ────────────────────────────────────────────────────────────────────────
    def upsert_node(self, node: GraphNodeRecord) -> None:
        with self._user_locks[node.user_id]:
            bucket = self._nodes[node.user_id]
            bucket[node.node_id] = node
            bucket.move_to_end(node.node_id)
            while len(bucket) > self.max_nodes_per_user:
                evicted_id, _ = bucket.popitem(last=False)
                # 同步移除邻接 + 相关边
                for eid in list(self._adj[node.user_id].pop(evicted_id, set())):
                    edge = self._edges[node.user_id].pop(eid, None)
                    if edge:
                        other = (
                            edge.target_node_id
                            if edge.source_node_id == evicted_id
                            else edge.source_node_id
                        )
                        self._adj[node.user_id][other].discard(eid)

    def get_node(self, user_id: str, node_id: str) -> GraphNodeRecord | None:
        with self._user_locks[user_id]:
            return self._nodes.get(user_id, {}).get(node_id)

    def find_nodes(
        self, user_id: str, filters: dict[str, Any] | None = None
    ) -> list[GraphNodeRecord]:
        with self._user_locks[user_id]:
            items = list(self._nodes.get(user_id, {}).values())
        if not filters:
            return items
        out: list[GraphNodeRecord] = []
        for n in items:
            ok = True
            for k, v in filters.items():
                cur = getattr(n, k, n.properties.get(k))
                if cur != v:
                    ok = False
                    break
            if ok:
                out.append(n)
        return out

    # ────────────────────────────────────────────────────────────────────────
    # 边
    # ────────────────────────────────────────────────────────────────────────
    def upsert_edge(self, edge: GraphEdgeRecord) -> None:
        with self._user_locks[edge.user_id]:
            bucket = self._edges[edge.user_id]
            existed = bucket.get(edge.edge_id)
            if existed is not None:
                # merge:strength += 1,confidence 取较大,source_memory_ids 合并
                existed.strength = float(existed.strength) + 1.0
                existed.confidence = max(existed.confidence, edge.confidence)
                for mid in edge.source_memory_ids:
                    if mid not in existed.source_memory_ids:
                        existed.source_memory_ids.append(mid)
                existed.is_valid = edge.is_valid
                existed.properties.update(edge.properties)
                bucket.move_to_end(edge.edge_id)
                return
            bucket[edge.edge_id] = edge
            bucket.move_to_end(edge.edge_id)
            self._adj[edge.user_id][edge.source_node_id].add(edge.edge_id)
            self._adj[edge.user_id][edge.target_node_id].add(edge.edge_id)
            while len(bucket) > self.max_edges_per_user:
                evicted_id, ev_edge = bucket.popitem(last=False)
                self._adj[edge.user_id][ev_edge.source_node_id].discard(evicted_id)
                self._adj[edge.user_id][ev_edge.target_node_id].discard(evicted_id)

    def update_edge(
        self, user_id: str, edge_id: str, updates: dict[str, Any]
    ) -> bool:
        with self._user_locks[user_id]:
            edge = self._edges.get(user_id, {}).get(edge_id)
            if edge is None:
                return False
            for k, v in updates.items():
                if hasattr(edge, k):
                    setattr(edge, k, v)
                else:
                    edge.properties[k] = v
            return True

    # ────────────────────────────────────────────────────────────────────────
    # 遍历
    # ────────────────────────────────────────────────────────────────────────
    def traverse(
        self,
        user_id: str,
        seed_node_ids: Iterable[str],
        max_hops: int = 2,
        edge_types: Iterable[str] | None = None,
    ) -> tuple[list[GraphNodeRecord], list[GraphEdgeRecord]]:
        """从 seed BFS 限界遍历。返回 ``(nodes, edges)`` 列表(去重)。"""
        max_hops = max(0, min(int(max_hops), 3))  # SPI 契约:>3 硬限制
        edge_types_set = {t.upper() for t in (edge_types or [])}
        # 在锁内对 nodes/edges/adj 拍快照(deepcopy 一次性把图本次遍历需要的子集
        # 复制出来),后续 BFS 在锁外运行;避免遍历期间被 upsert 改坏迭代器。
        with self._user_locks[user_id]:
            nodes_snapshot = dict(self._nodes.get(user_id, {}))
            edges_snapshot = dict(self._edges.get(user_id, {}))
            adj_snapshot = {
                nid: set(eids) for nid, eids in self._adj.get(user_id, {}).items()
            }
        visited_nodes: set[str] = set()
        visited_edges: set[str] = set()
        out_nodes: list[GraphNodeRecord] = []
        out_edges: list[GraphEdgeRecord] = []

        frontier: list[str] = []
        for sid in seed_node_ids or []:
            if sid not in visited_nodes:
                visited_nodes.add(sid)
                node = nodes_snapshot.get(sid)
                if node is not None and node.is_valid:
                    out_nodes.append(node)
                frontier.append(sid)

        for _hop in range(max_hops):
            next_frontier: list[str] = []
            for nid in frontier:
                for eid in adj_snapshot.get(nid, set()):
                    if eid in visited_edges:
                        continue
                    edge = edges_snapshot.get(eid)
                    if edge is None or not edge.is_valid:
                        continue
                    if edge_types_set and edge.edge_type.upper() not in edge_types_set:
                        continue
                    visited_edges.add(eid)
                    out_edges.append(edge)
                    other = (
                        edge.target_node_id
                        if edge.source_node_id == nid
                        else edge.source_node_id
                    )
                    if other in visited_nodes:
                        continue
                    visited_nodes.add(other)
                    node = nodes_snapshot.get(other)
                    if node is not None and node.is_valid:
                        out_nodes.append(node)
                    next_frontier.append(other)
            if not next_frontier:
                break
            frontier = next_frontier
        return out_nodes, out_edges

    # ────────────────────────────────────────────────────────────────────────
    # 监控
    # ────────────────────────────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        return {
            "users": len(self._nodes),
            "total_nodes": sum(len(b) for b in self._nodes.values()),
            "total_edges": sum(len(b) for b in self._edges.values()),
        }


# ════════════════════════════════════════════════════════════════════════════
# 业务门面
# ════════════════════════════════════════════════════════════════════════════
class MemoryGraph:
    """业务层包装:把"记忆 + 实体列表" 写入图,支持邻居 / 相关记忆查询。

    构造接收**鸭子类型** ``store`` —— 提供 ``upsert_node`` / ``upsert_edge`` /
    ``get_node`` / ``traverse``。生产传入 :class:`InMemoryLRUGraphStore`
    或其他 GraphStore SPI 实例。
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    # ────────────────────────────────────────────────────────────────────────
    # 写入
    # ────────────────────────────────────────────────────────────────────────
    async def add_memory_node(
        self,
        mem_cell_id: str,
        entities: Iterable[str],
        tenant_id: str,
        user_id: str,
    ) -> dict[str, int]:
        """写入 memory 节点 + 每个 entity 节点 + ``MENTIONS`` 边。

        :returns: ``{entity_count, edge_count}``
        """
        from memory_app.entity_store import _dedupe_entities

        ents = _dedupe_entities(entities)
        mid = memory_node_id(mem_cell_id)
        await _maybe_await(
            self.store.upsert_node(
                GraphNodeRecord(
                    node_id=mid,
                    node_type="memory",
                    user_id=user_id,
                    label=mem_cell_id,
                    memory_id=mem_cell_id,
                    properties={"tenant_id": tenant_id},
                )
            )
        )
        edge_count = 0
        for ent in ents:
            eid = entity_node_id(tenant_id, user_id, ent)
            await _maybe_await(
                self.store.upsert_node(
                    GraphNodeRecord(
                        node_id=eid,
                        node_type="entity",
                        user_id=user_id,
                        label=ent,
                        properties={"tenant_id": tenant_id, "name": ent},
                    )
                )
            )
            await _maybe_await(
                self.store.upsert_edge(
                    GraphEdgeRecord(
                        edge_id=edge_id(mid, "MENTIONS", eid),
                        edge_type="MENTIONS",
                        source_node_id=mid,
                        target_node_id=eid,
                        user_id=user_id,
                        confidence=0.9,
                        strength=1.0,
                        source_memory_ids=[mem_cell_id],
                    )
                )
            )
            edge_count += 1
        return {"entity_count": len(ents), "edge_count": edge_count}

    # ────────────────────────────────────────────────────────────────────────
    # 查询
    # ────────────────────────────────────────────────────────────────────────
    async def get_neighbors(
        self,
        user_id: str,
        node_id: str,
        max_depth: int = 2,
    ) -> list[str]:
        """BFS,返回 ``node_id`` 邻域中 ``memory`` 类型节点的 ``mem_cell_id`` 列表。"""
        nodes, _edges = await _maybe_await(
            self.store.traverse(
                user_id=user_id,
                seed_node_ids=[node_id],
                max_hops=max_depth,
                edge_types=None,
            )
        )
        out: list[str] = []
        seen: set[str] = set()
        for n in nodes:
            if n.node_type != "memory":
                continue
            mid = n.memory_id or n.label
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    async def find_related_memories(
        self,
        tenant_id: str,
        user_id: str,
        entity: str,
        max_depth: int = 2,
    ) -> list[str]:
        """便利:从实体名出发,返回相关 mem_cell_id 列表。"""
        eid = entity_node_id(tenant_id, user_id, entity)
        return await self.get_neighbors(user_id, eid, max_depth=max_depth)


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
async def _maybe_await(value):
    """兼容同步 / 异步注入的 store。"""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "GraphNodeRecord",
    "GraphEdgeRecord",
    "InMemoryGraph",
    "MemoryGraph",
    "entity_node_id",
    "memory_node_id",
    "edge_id",
]
