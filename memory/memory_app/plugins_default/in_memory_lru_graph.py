"""``in_memory_lru_graph`` —— 图与实体 默认 GraphStore 插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.graph_store.GraphStore` 的内存实现。
内部委托 :class:`memory_app.graph_index.InMemoryGraph`。

═══════════════════════════════════════════════════════════════════════════════
适用场景
═══════════════════════════════════════════════════════════════════════════════
- 图与实体 起把图能力打开,但不强依赖 Neo4j / Nebula
- 单机部署 / 测试 / 评测;进程重启即丢
- 生产建议替换为 ``neo4j_graph_store`` / ``nebula_graph_store``

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    max_nodes_per_user: int 默认 4096
    max_edges_per_user: int 默认 8192
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.graph_index import (
    GraphEdgeRecord,
    GraphNodeRecord,
    InMemoryGraph,
)
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.graph_store import (
    GraphEdge as SPIGraphEdge,
    GraphNode as SPIGraphNode,
    GraphStore,
)

logger = logging.getLogger(__name__)


@register
class InMemoryLRUGraphStore(GraphStore):
    """内存 LRU GraphStore(图与实体 默认)。"""

    meta = PluginMeta(
        name="in_memory_lru_graph",
        category="memory.storage.graph_store",
        version="1.0.0",
        description="内存 + LRU 上限的 GraphStore;按 user_id 分片",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "max_nodes_per_user": {
                    "type": "integer", "minimum": 16, "maximum": 1_000_000,
                    "default": 4096,
                },
                "max_edges_per_user": {
                    "type": "integer", "minimum": 16, "maximum": 2_000_000,
                    "default": 8192,
                },
            },
        },
    )

    def __init__(self) -> None:
        self._graph: InMemoryGraph = InMemoryGraph()

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._graph = InMemoryGraph(
            max_nodes_per_user=int(config.get("max_nodes_per_user", 4096)),
            max_edges_per_user=int(config.get("max_edges_per_user", 8192)),
        )
        logger.info(
            "in_memory_lru_graph started: max_nodes=%d, max_edges=%d",
            self._graph.max_nodes_per_user, self._graph.max_edges_per_user,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        st = self._graph.stats()
        return {"status": "ok", "detail": f"{st}"}

    async def metrics(self) -> dict:
        st = self._graph.stats()
        return {f"graph_{k}": v for k, v in st.items()}

    # ────────────────────────────────────────────────────────────────────────
    # 业务便利:暴露内核(让 MemoryGraph 直接调)
    # ────────────────────────────────────────────────────────────────────────
    @property
    def core(self) -> InMemoryGraph:
        return self._graph

    # 兼容 :class:`MemoryGraph` 鸭子类型(同步接口,_maybe_await 自动适配)
    def upsert_node(self, node: GraphNodeRecord) -> None:
        self._graph.upsert_node(node)

    def upsert_edge(self, edge: GraphEdgeRecord) -> None:
        self._graph.upsert_edge(edge)

    # ────────────────────────────────────────────────────────────────────────
    # SPI:节点
    # ────────────────────────────────────────────────────────────────────────
    async def add_node(self, node: SPIGraphNode) -> None:
        self._graph.upsert_node(_spi_to_record_node(node))

    async def get_node(self, user_id: str, node_id: str) -> SPIGraphNode | None:
        rec = self._graph.get_node(user_id, node_id)
        if rec is None:
            return None
        return _record_to_spi_node(rec)

    async def find_nodes(self, user_id: str, filters: dict) -> list[SPIGraphNode]:
        return [
            _record_to_spi_node(n)
            for n in self._graph.find_nodes(user_id, filters or None)
        ]

    # ────────────────────────────────────────────────────────────────────────
    # SPI:边
    # ────────────────────────────────────────────────────────────────────────
    async def add_edge(self, edge: SPIGraphEdge) -> None:
        self._graph.upsert_edge(_spi_to_record_edge(edge))

    async def update_edge(self, user_id: str, edge_id: str, updates: dict) -> bool:
        return self._graph.update_edge(user_id, edge_id, updates or {})

    # ────────────────────────────────────────────────────────────────────────
    # SPI:遍历
    # ────────────────────────────────────────────────────────────────────────
    async def traverse(
        self,
        user_id: str,
        seed_node_ids: list[str],
        max_hops: int = 2,
        edge_types: list[str] | None = None,
    ) -> tuple[list[SPIGraphNode], list[SPIGraphEdge]]:
        nodes, edges = self._graph.traverse(
            user_id=user_id,
            seed_node_ids=seed_node_ids,
            max_hops=max_hops,
            edge_types=edge_types,
        )
        return (
            [_record_to_spi_node(n) for n in nodes],
            [_record_to_spi_edge(e) for e in edges],
        )


# ════════════════════════════════════════════════════════════════════════════
# 互转
# ════════════════════════════════════════════════════════════════════════════
def _spi_to_record_node(node: SPIGraphNode) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_id=node.id,
        node_type=node.node_type,
        user_id=node.user_id,
        label=node.label,
        memory_id=node.memory_id,
        is_valid=node.is_valid,
        properties=dict(node.extra_json or {}),
    )


def _record_to_spi_node(rec: GraphNodeRecord) -> SPIGraphNode:
    return SPIGraphNode(
        id=rec.node_id,
        node_type=rec.node_type,
        label=rec.label,
        user_id=rec.user_id,
        memory_id=rec.memory_id,
        is_valid=rec.is_valid,
        extra_json=dict(rec.properties or {}),
    )


def _spi_to_record_edge(edge: SPIGraphEdge) -> GraphEdgeRecord:
    # SPI 没有 user_id 字段;按约定调用方必须在 ``extra_json["user_id"]`` 中提供。
    # 缺失时显式抛错 —— 静默退化为 ``user_id=""`` 会让 ``traverse(user_id=X)``
    # 永远找不到这些边,造成静默数据丢失。
    user_id = (edge.extra_json or {}).get("user_id")
    if not user_id:
        raise ValueError(
            f"GraphEdge {edge.id!r} missing required user_id in extra_json; "
            f"set extra_json['user_id'] before add_edge to ensure user-scoped traversal."
        )
    return GraphEdgeRecord(
        edge_id=edge.id,
        edge_type=edge.edge_type,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        user_id=str(user_id),
        confidence=float(edge.confidence),
        strength=float(edge.strength),
        source_memory_ids=list(edge.source_memory_ids or []),
        is_valid=edge.is_valid,
        properties=dict(edge.extra_json or {}),
    )


def _record_to_spi_edge(rec: GraphEdgeRecord) -> SPIGraphEdge:
    extra = dict(rec.properties or {})
    extra["user_id"] = rec.user_id
    return SPIGraphEdge(
        id=rec.edge_id,
        edge_type=rec.edge_type,
        source_node_id=rec.source_node_id,
        target_node_id=rec.target_node_id,
        confidence=rec.confidence,
        strength=rec.strength,
        source_memory_ids=list(rec.source_memory_ids or []),
        is_valid=rec.is_valid,
        extra_json=extra,
    )


__all__ = ["InMemoryLRUGraphStore"]
