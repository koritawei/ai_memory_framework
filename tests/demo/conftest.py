"""Demo 测试共享 fakes —— Mongo / ES / Milvus 内存版。

═══════════════════════════════════════════════════════════════════════════════
为什么再写一份 fake
═══════════════════════════════════════════════════════════════════════════════
tests/test_three_store_sync.py 里也有 _FakeMongoRepo / _FakeESClient,但
那一份的方法集只够 ingest 管线用。demo 跨多个流程,需要更完整的接口:

- FakeMongoRepo 必须实现 ``atomic_apply_strength_delta``(feedback / lifecycle 用)
  + ``get_by_ids`` / ``find_all`` / ``find_by_state`` / ``count`` /
  ``bulk_increment_access`` / ``bulk_set_state``
- FakeESClient 多了 ``search``(给 BM25 通道用)
- 引入 FakeEntityStore + FakeMemoryGraph(给 cold path / 检索 entity 通道用)

把 fake 集中在 demo/conftest.py 里,避免污染主测试套件的 fixture 命名空间。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import pytest

from memory_app.internal_models import MemCell, MemoryState


# ════════════════════════════════════════════════════════════════════════════
# FakeMongoRepo —— 完整模拟 MongoMemCellRepo 的所有 demo 需要的方法
# ════════════════════════════════════════════════════════════════════════════
class FakeMongoRepo:
    """内存 dict + 原子化 ``$set`` pipeline 语义。

    与真实 ``MongoMemCellRepo`` 等价的方法清单:

    - ``insert(cell)`` / ``get_by_id(mid)`` / ``get_by_ids(ids)``
    - ``update(mid, dict)`` —— 部分字段覆盖
    - ``find_all(tenant, user, limit)`` / ``find_by_state(tenant, user, state)``
    - ``count(tenant, user)``
    - ``bulk_set_state(ids, state)`` / ``bulk_increment_access(ids, ...)``
    - ``atomic_apply_strength_delta(mid, delta, s_max, increment_access)``
      —— 关键:模拟 Mongo 4.2+ aggregation-pipeline ``$set`` 行为,
      Python 端没有 read-modify-write 窗口,demo 才能演示并发反馈不丢更新。

    每次调用都会记录在对应属性上(``inserts`` / ``updates`` / ``atomic_calls``),
    断言时可以直接读。
    """

    def __init__(self) -> None:
        self.store: dict[str, MemCell] = {}
        # 调用记录(便于 demo 断言)
        self.inserts: list[str] = []
        self.updates: list[tuple[str, dict]] = []
        self.atomic_calls: list[tuple[str, float, float, bool]] = []
        self.bulk_increment_calls: list[list[str]] = []

    # ── 写入 ──────────────────────────────────────────────────────────────
    async def insert(self, cell: MemCell) -> str:
        self.store[cell.mem_cell_id] = cell
        self.inserts.append(cell.mem_cell_id)
        return cell.mem_cell_id

    async def insert_many(self, cells: Iterable[MemCell]) -> list[str]:
        out: list[str] = []
        for c in cells:
            out.append(await self.insert(c))
        return out

    # ── 读取 ──────────────────────────────────────────────────────────────
    async def get_by_id(self, mem_cell_id: str) -> MemCell | None:
        return self.store.get(mem_cell_id)

    async def get_by_ids(
        self,
        mem_cell_ids: list[str],
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> list[MemCell]:
        """与真实 repo 同语义:保留入参顺序,不存在则丢弃。"""
        out: list[MemCell] = []
        for mid in mem_cell_ids:
            cell = self.store.get(mid)
            if cell is None:
                continue
            if tenant_id is not None and cell.tenant_id != tenant_id:
                continue
            if user_id is not None and cell.user_id != user_id:
                continue
            out.append(cell)
        return out

    async def find_all(
        self, tenant_id: str, user_id: str, limit: int = 10000
    ) -> list[MemCell]:
        results = [
            c for c in self.store.values()
            if c.tenant_id == tenant_id and c.user_id == user_id
        ]
        return results[:limit]

    async def find_by_state(
        self,
        tenant_id: str,
        user_id: str,
        state: Any,
        limit: int = 1000,
    ) -> list[MemCell]:
        state_value = state.value if hasattr(state, "value") else str(state)
        results = [
            c for c in self.store.values()
            if c.tenant_id == tenant_id
            and c.user_id == user_id
            and (c.state.value if hasattr(c.state, "value") else str(c.state)) == state_value
        ]
        return results[:limit]

    async def count(self, tenant_id: str, user_id: str) -> int:
        return sum(
            1 for c in self.store.values()
            if c.tenant_id == tenant_id and c.user_id == user_id
        )

    # ── 更新 ──────────────────────────────────────────────────────────────
    async def update(self, mem_cell_id: str, updates: dict[str, Any], **_scope) -> bool:
        cell = self.store.get(mem_cell_id)
        if cell is None:
            return False
        self.updates.append((mem_cell_id, dict(updates)))
        for k, v in updates.items():
            if k == "state" and isinstance(v, str):
                # 与 Mongo 行为一致:state value 是字符串
                try:
                    v = MemoryState(v)
                except ValueError:
                    pass
            try:
                setattr(cell, k, v)
            except Exception:  # noqa: BLE001
                pass  # extra="allow" 已宽松,这里再兜一次
        return True

    async def bulk_set_state(
        self, mem_cell_ids: list[str], new_state: Any
    ) -> int:
        state_value = new_state.value if hasattr(new_state, "value") else str(new_state)
        affected = 0
        for mid in mem_cell_ids:
            cell = self.store.get(mid)
            if cell is None:
                continue
            try:
                cell.state = MemoryState(state_value)
            except ValueError:
                pass
            cell.updated_at = datetime.now(timezone.utc)
            affected += 1
        return affected

    async def bulk_increment_access(
        self,
        mem_cell_ids: list[str],
        *,
        strength_delta: float,
        s_max: float,
        **_scope,
    ) -> int:
        """与真实 repo 的 aggregation pipeline 语义对齐。"""
        self.bulk_increment_calls.append(list(mem_cell_ids))
        affected = 0
        for mid in mem_cell_ids:
            cell = self.store.get(mid)
            if cell is None:
                continue
            cell.strength = min(s_max, float(cell.strength) + float(strength_delta))
            cell.access_count = int(cell.access_count) + 1
            cell.updated_at = datetime.now(timezone.utc)
            affected += 1
        return affected

    # ── 原子化反馈/生命周期 ─────────────────────────────────────────────
    async def atomic_apply_strength_delta(
        self,
        mem_cell_id: str,
        *,
        delta: float,
        s_max: float,
        increment_access: bool,
        **_scope,
    ) -> dict | None:
        """**关键 fake**:模拟服务端 ``find_one_and_update`` + aggregation $set。

        语义保证:Python 端没有 read-modify-write 窗口 —— 即使 demo 用
        ``asyncio.gather`` 并发触发,每次"读旧值 + 算新值 + 写入"也是一次
        原子操作,不会出现"两个并发请求都基于陈旧值更新,最后只生效一次"的
        丢更新场景。这正是真实 Mongo 的契约。
        """
        self.atomic_calls.append((mem_cell_id, delta, s_max, increment_access))
        cell = self.store.get(mem_cell_id)
        if cell is None:
            return None
        # 真实 Mongo 在 $set 阶段一气呵成;Python 端按序执行模拟原子性
        cell.strength = min(s_max, float(cell.strength) + float(delta))
        if increment_access:
            cell.access_count = int(cell.access_count) + 1
        cell.updated_at = datetime.now(timezone.utc)
        return {
            "strength": float(cell.strength),
            "access_count": int(cell.access_count),
        }


# ════════════════════════════════════════════════════════════════════════════
# FakeESClient —— 给 ES BM25 通道 / SyncIndexStage 用
# ════════════════════════════════════════════════════════════════════════════
class FakeESClient:
    """模拟 ``elasticsearch.AsyncElasticsearch`` 的最小子集。"""

    def __init__(self, index_should_fail: bool = False) -> None:
        self.indexed: list[tuple[str, str, dict]] = []  # (index, id, doc)
        self.deleted: list[tuple[str, str]] = []
        self.fail = index_should_fail
        self.indices = self._FakeIndices()

    class _FakeIndices:
        def __init__(self) -> None:
            self.created: list[str] = []

        async def exists(self, index: str) -> bool:
            return index in self.created

        async def create(self, index: str, mappings=None) -> None:
            self.created.append(index)

    async def index(self, index: str, id: str, document: dict) -> None:
        if self.fail:
            raise RuntimeError("ES down (demo fake)")
        self.indexed.append((index, id, document))

    async def delete(self, index: str, id: str, ignore=None) -> None:
        self.deleted.append((index, id))


# ════════════════════════════════════════════════════════════════════════════
# FakeMilvusInsert —— SyncIndexStage 注入的可调用对象
# ════════════════════════════════════════════════════════════════════════════
class FakeMilvusInsert:
    """记录 Milvus insert 调用,可设 ``fail=True`` 触发 DLQ 演示。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[float], dict]] = []
        self.fail = fail

    async def __call__(
        self, mid: str, embedding: list[float], metadata: dict
    ) -> None:
        if self.fail:
            raise RuntimeError("Milvus down (demo fake)")
        self.calls.append((mid, list(embedding), dict(metadata)))


# ════════════════════════════════════════════════════════════════════════════
# FakeEntityStore —— EntityIndexStage + EntityChannel 共用
# ════════════════════════════════════════════════════════════════════════════
class FakeEntityStore:
    """``{(tenant, user, entity)} → set[mem_cell_id]`` 倒排索引。"""

    def __init__(self) -> None:
        self.index: dict[tuple[str, str, str], set[str]] = {}
        self.upsert_calls: list[tuple[str, list[str]]] = []  # (mem_cell_id, entities)

    async def upsert_entities(
        self,
        mem_cell_id: str,
        entities: Iterable[str],
        tenant_id: str,
        user_id: str,
    ) -> int:
        ents = [e for e in entities if e]
        self.upsert_calls.append((mem_cell_id, list(ents)))
        for ent in ents:
            self.index.setdefault((tenant_id, user_id, ent), set()).add(mem_cell_id)
        return len(ents)

    async def find_by_entities(
        self,
        entities: Iterable[str],
        tenant_id: str,
        user_id: str,
        *,
        limit: int = 1000,
    ) -> list[str]:
        out: set[str] = set()
        for ent in entities:
            out.update(self.index.get((tenant_id, user_id, ent), set()))
        return list(out)[:limit]


# ════════════════════════════════════════════════════════════════════════════
# FakeMemoryGraph —— GraphChannel + EntityIndexStage 共用
# ════════════════════════════════════════════════════════════════════════════
class FakeMemoryGraph:
    """记录 ``add_memory_node`` 调用 + 简单图遍历。

    内部不实现真正的 BFS,demo 只要断言"写入路径走到这里了"即可;
    需要测真 BFS 的去用 ``InMemoryGraph`` 直接构造。
    """

    def __init__(self) -> None:
        self.add_memory_calls: list[tuple[str, list[str], str, str]] = []
        # entity_node_id → set[memory_id]
        self.adj: dict[str, set[str]] = {}

    async def add_memory_node(
        self,
        mem_cell_id: str,
        entities: Iterable[str],
        tenant_id: str,
        user_id: str,
    ) -> dict[str, int]:
        ents = list(entities)
        self.add_memory_calls.append((mem_cell_id, ents, tenant_id, user_id))
        for ent in ents:
            ent_node = f"entity:{tenant_id}:{user_id}:{ent}"
            self.adj.setdefault(ent_node, set()).add(mem_cell_id)
        return {"entity_count": len(ents), "edge_count": len(ents)}

    async def get_neighbors(
        self, user_id: str, node_id: str, max_depth: int = 2
    ) -> list[str]:
        return list(self.adj.get(node_id, set()))

    async def find_related_memories(
        self,
        tenant_id: str,
        user_id: str,
        entity: str,
        max_depth: int = 2,
    ) -> list[str]:
        node = f"entity:{tenant_id}:{user_id}:{entity}"
        return list(self.adj.get(node, set()))


# ════════════════════════════════════════════════════════════════════════════
# 便利:构造 MemCell 的 helper
# ════════════════════════════════════════════════════════════════════════════
def make_cell(
    text: str,
    *,
    tenant_id: str = "t1",
    user_id: str = "u1",
    embedding: list[float] | None = None,
    strength: float = 1.0,
    access_count: int = 0,
    state: MemoryState = MemoryState.ACTIVE,
) -> MemCell:
    """构造一条 MemCell;只为 demo 简化样板。"""
    kwargs: dict[str, Any] = dict(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id="demo-session",
        text=text,
        strength=strength,
        access_count=access_count,
        state=state,
    )
    if embedding is not None:
        kwargs["embedding"] = list(embedding)
    return MemCell(**kwargs)


# ════════════════════════════════════════════════════════════════════════════
# Pytest fixtures
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def fake_mongo() -> FakeMongoRepo:
    return FakeMongoRepo()


@pytest.fixture
def fake_es() -> FakeESClient:
    return FakeESClient()


@pytest.fixture
def fake_milvus() -> FakeMilvusInsert:
    return FakeMilvusInsert()


@pytest.fixture
def fake_entity_store() -> FakeEntityStore:
    return FakeEntityStore()


@pytest.fixture
def fake_memory_graph() -> FakeMemoryGraph:
    return FakeMemoryGraph()
