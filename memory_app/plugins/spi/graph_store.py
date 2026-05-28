"""GraphStore SPI —— 图存储。

默认实现 ``in_memory_lru_graph``（按 user_id 分片 + LRU 淘汰）；
可换 ``neo4j_graph_store`` / ``nebula_graph_store``（生产场景）。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel, ConfigDict

from memory_app.plugins.base import Plugin


class GraphNode(BaseModel):
    """图节点。"""

    model_config = ConfigDict(extra="allow")

    id: str
    node_type: str  # episode / semantic / entity / community
    label: str
    user_id: str
    memory_id: str | None = None  # 关联的记忆 ID（若有）
    is_valid: bool = True
    extra_json: dict = {}


class GraphEdge(BaseModel):
    """图边。"""

    model_config = ConfigDict(extra="allow")

    id: str
    edge_type: str  # MENTIONS / SUPPORTS / CONFLICTS / UPDATES / RELATED_TO / BELONGS_TO
    source_node_id: str
    target_node_id: str
    confidence: float = 1.0
    strength: float = 1.0
    source_memory_ids: list[str] = []  # 必填：缺失则该边不能进检索上下文
    valid_at: str | None = None
    invalid_at: str | None = None
    is_valid: bool = True
    extra_json: dict = {}


class GraphStore(Plugin):
    """图存储扩展点。"""

    # ── 节点 ──
    @abstractmethod
    async def add_node(self, node: GraphNode) -> None: ...

    @abstractmethod
    async def get_node(self, user_id: str, node_id: str) -> GraphNode | None: ...

    @abstractmethod
    async def find_nodes(self, user_id: str, filters: dict) -> list[GraphNode]: ...

    # ── 边 ──
    @abstractmethod
    async def add_edge(self, edge: GraphEdge) -> None: ...

    @abstractmethod
    async def update_edge(self, user_id: str, edge_id: str, updates: dict) -> bool:
        """更新边的 strength / valid_at / invalid_at 等字段。"""

    # ── 遍历 ──
    @abstractmethod
    async def traverse(
        self,
        user_id: str,
        seed_node_ids: list[str],
        max_hops: int = 2,
        edge_types: list[str] | None = None,
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        """从 seed 节点限界扩展，返回路径上的节点与边集合。

        约定：
        - 必须按 user_id 分片，**禁止**跨用户图遍历
        - 应过滤 ``is_valid=false`` 与 ``invalid_at`` 已过期的边
        - 默认 max_hops=2 控制噪声；> 3 会被实现内部硬限制
        """


__all__ = ["GraphStore", "GraphNode", "GraphEdge"]
