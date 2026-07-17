"""MongoMemCellRepo 测试(Step 2.2)。"""

from __future__ import annotations

from typing import Any

import pytest

from memory_app.internal_models import MemCell
from memory_app.repositories.mongo_repo import MongoMemCellRepo


# ════════════════════════════════════════════════════════════════════════════
# Fake motor 替身:用 dict 模拟 collection,避免拉真实 Mongo
# ════════════════════════════════════════════════════════════════════════════
class _FakeResult:
    def __init__(self, modified: int = 0, deleted: int = 0) -> None:
        self.modified_count = modified
        self.deleted_count = deleted


def _eval_agg_expr(expr: Any, doc: dict[str, Any]) -> Any:
    """最小 aggregation 表达式求值(覆盖 atomic_apply_strength_delta 用到的算子)。"""
    if isinstance(expr, dict):
        if "$ifNull" in expr:
            field, default = expr["$ifNull"]
            if isinstance(field, str) and field.startswith("$"):
                val = doc.get(field[1:])
            else:
                val = _eval_agg_expr(field, doc)
            return default if val is None else val
        if "$add" in expr:
            return sum(float(_eval_agg_expr(x, doc)) for x in expr["$add"])
        if "$min" in expr:
            return min(float(_eval_agg_expr(x, doc)) for x in expr["$min"])
    return expr


def _apply_pipeline_set(doc: dict[str, Any], sets: dict[str, Any]) -> None:
    for key, expr in sets.items():
        doc[key] = _eval_agg_expr(expr, doc)


def _doc_matches_filter(doc: dict[str, Any], filt: dict[str, Any]) -> bool:
    mid = filt.get("mem_cell_id")
    if mid is not None and doc.get("mem_cell_id") != mid:
        return False
    for key in ("tenant_id", "user_id"):
        if key in filt and doc.get(key) != filt[key]:
            return False
    return True


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.created_indexes: list[tuple] = []
        self.insert_many_calls: list[list[dict]] = []

    async def create_index(self, keys, **kwargs):
        self.created_indexes.append((tuple(keys), kwargs))

    async def insert_one(self, doc):
        if doc["mem_cell_id"] in self.docs:
            raise RuntimeError("duplicate key")
        self.docs[doc["mem_cell_id"]] = dict(doc)
        return _FakeResult()

    async def insert_many(self, docs, ordered: bool = True):
        docs = list(docs)
        self.insert_many_calls.append(docs)
        for d in docs:
            self.docs[d["mem_cell_id"]] = dict(d)
        return _FakeResult()

    async def find_one(self, filt):
        mid = filt.get("mem_cell_id")
        if mid not in self.docs:
            return None
        doc = self.docs[mid]
        if not _doc_matches_filter(doc, filt):
            return None
        return dict(doc)

    def find(self, filt):
        """Minimal find for get_by_ids tests."""

        class _Cursor:
            def __init__(self, docs):
                self._docs = docs

            async def to_list(self, length=None):
                return list(self._docs)

        ids = filt.get("mem_cell_id", {})
        if isinstance(ids, dict) and "$in" in ids:
            wanted = ids["$in"]
            out = []
            for mid in wanted:
                if mid not in self.docs:
                    continue
                doc = self.docs[mid]
                if "tenant_id" in filt and doc.get("tenant_id") != filt["tenant_id"]:
                    continue
                if "user_id" in filt and doc.get("user_id") != filt["user_id"]:
                    continue
                out.append(dict(doc))
            return _Cursor(out)
        return _Cursor([])

    def _apply_update(self, doc: dict[str, Any], update: Any) -> None:
        if isinstance(update, list):
            for stage in update:
                if "$set" in stage:
                    _apply_pipeline_set(doc, stage["$set"])
            return
        if isinstance(update, dict) and "$set" in update:
            doc.update(update["$set"])

    async def find_one_and_update(self, filt, update, return_document=None, **_kwargs):
        mid = filt.get("mem_cell_id")
        if mid not in self.docs:
            return None
        doc = self.docs[mid]
        if not _doc_matches_filter(doc, filt):
            return None
        self._apply_update(doc, update)
        return dict(doc)

    async def update_one(self, filt, update):
        mid = filt.get("mem_cell_id")
        if mid not in self.docs:
            return _FakeResult(modified=0)
        doc = self.docs[mid]
        if not _doc_matches_filter(doc, filt):
            return _FakeResult(modified=0)
        self._apply_update(doc, update)
        return _FakeResult(modified=1)

    async def delete_one(self, filt):
        mid = filt.get("mem_cell_id")
        if mid not in self.docs:
            return _FakeResult(deleted=0)
        doc = self.docs[mid]
        if not _doc_matches_filter(doc, filt):
            return _FakeResult(deleted=0)
        del self.docs[mid]
        return _FakeResult(deleted=1)


class _FakeDB:
    def __init__(self) -> None:
        self._collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        if name not in self._collections:
            self._collections[name] = _FakeCollection()
        return self._collections[name]


# ════════════════════════════════════════════════════════════════════════════
# fixtures
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def fake_db():
    return _FakeDB()


@pytest.fixture
def repo(fake_db):
    return MongoMemCellRepo(fake_db, collection_name="mem_cells")


def _cell(text: str = "hello", **overrides) -> MemCell:
    base = dict(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        raw_data_ids=["r1"],
        text=text,
    )
    base.update(overrides)
    return MemCell(**base)


# ════════════════════════════════════════════════════════════════════════════
# 测试
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestMongoMemCellRepo:
    async def test_insert_returns_id(self, repo: MongoMemCellRepo):
        cell = _cell("hi")
        mid = await repo.insert(cell)
        assert mid == cell.mem_cell_id

    async def test_get_by_id_round_trip(self, repo: MongoMemCellRepo):
        cell = _cell("round trip")
        await repo.insert(cell)
        got = await repo.get_by_id(cell.mem_cell_id)
        assert got is not None
        assert got.mem_cell_id == cell.mem_cell_id
        assert got.text == "round trip"

    async def test_get_by_id_missing(self, repo: MongoMemCellRepo):
        got = await repo.get_by_id("non-existent")
        assert got is None

    async def test_update(self, repo: MongoMemCellRepo):
        cell = _cell("orig")
        await repo.insert(cell)
        ok = await repo.update(cell.mem_cell_id, {"summary": "new summary"})
        assert ok is True
        got = await repo.get_by_id(cell.mem_cell_id)
        assert got.summary == "new summary"

    async def test_update_missing_returns_false(self, repo: MongoMemCellRepo):
        ok = await repo.update("nope", {"summary": "x"})
        assert ok is False

    async def test_delete_by_id(self, repo: MongoMemCellRepo):
        cell = _cell()
        await repo.insert(cell)
        ok = await repo.delete_by_id(cell.mem_cell_id)
        assert ok is True
        assert await repo.get_by_id(cell.mem_cell_id) is None

    async def test_delete_missing_returns_false(self, repo: MongoMemCellRepo):
        ok = await repo.delete_by_id("not-here")
        assert ok is False

    async def test_insert_many(self, repo: MongoMemCellRepo):
        cells = [_cell(f"m{i}") for i in range(3)]
        ids = await repo.insert_many(cells)
        assert len(ids) == 3
        for c in cells:
            got = await repo.get_by_id(c.mem_cell_id)
            assert got is not None

    async def test_insert_many_empty(self, repo: MongoMemCellRepo):
        ids = await repo.insert_many([])
        assert ids == []

    async def test_insert_many_partial_failure_returns_inserted_ids(self, repo: MongoMemCellRepo):
        class _PartialBulkError(Exception):
            def __init__(self, failed_index: int):
                self.details = {"writeErrors": [{"index": failed_index}]}

        cells = [_cell("a"), _cell("b"), _cell("c")]
        original = repo.collection.insert_many

        async def flaky_insert_many(docs, ordered=False):
            _ = ordered
            if len(docs) == 3:
                raise _PartialBulkError(1)
            return await original(docs, ordered=ordered)

        repo.collection.insert_many = flaky_insert_many  # type: ignore[method-assign]
        ids = await repo.insert_many(cells)
        assert len(ids) == 2
        assert cells[1].mem_cell_id not in ids
        assert cells[0].mem_cell_id in ids
        assert cells[2].mem_cell_id in ids

    async def test_ensure_indexes(self, repo: MongoMemCellRepo, fake_db):
        await repo.ensure_indexes()
        coll = fake_db["mem_cells"]
        # 至少建了 3 个索引
        assert len(coll.created_indexes) >= 3
        # 主键唯一索引存在
        assert any(
            idx[0] == (("mem_cell_id", 1),) and idx[1].get("unique") is True
            for idx in coll.created_indexes
        )

    async def test_ensure_indexes_swallows_errors(self, repo: MongoMemCellRepo, fake_db):
        """create_index 抛错时仅 warn,不向上传播。"""

        async def raising_create(*a, **kw):
            raise RuntimeError("mongo down")

        fake_db["mem_cells"].create_index = raising_create
        # 不应抛
        await repo.ensure_indexes()

    async def test_get_by_id_scoped_rejects_wrong_tenant(self, repo: MongoMemCellRepo):
        cell = _cell()
        await repo.insert(cell)
        got = await repo.get_by_id_scoped(
            cell.mem_cell_id, tenant_id="other", user_id=cell.user_id
        )
        assert got is None

    async def test_get_by_ids_scoped_filters_cross_user(self, repo: MongoMemCellRepo):
        cell_a = _cell(text="a", user_id="u1")
        cell_b = _cell(text="b", user_id="u2")
        await repo.insert(cell_a)
        await repo.insert(cell_b)
        got = await repo.get_by_ids(
            [cell_a.mem_cell_id, cell_b.mem_cell_id],
            tenant_id="t1",
            user_id="u1",
        )
        assert [c.mem_cell_id for c in got] == [cell_a.mem_cell_id]

    async def test_get_by_ids_partial_scope_returns_empty(self, repo: MongoMemCellRepo):
        cell_a = _cell(text="a", user_id="u1")
        cell_b = _cell(text="b", user_id="u2")
        await repo.insert(cell_a)
        await repo.insert(cell_b)
        got = await repo.get_by_ids(
            [cell_a.mem_cell_id, cell_b.mem_cell_id],
            tenant_id="t1",
        )
        assert got == []

    async def test_update_scoped_noop_on_mismatch(self, repo: MongoMemCellRepo):
        cell = _cell()
        await repo.insert(cell)
        ok = await repo.update(
            cell.mem_cell_id,
            {"summary": "nope"},
            tenant_id="t1",
            user_id="wrong-user",
        )
        assert ok is False
        got = await repo.get_by_id(cell.mem_cell_id)
        assert got.summary != "nope"


@pytest.mark.asyncio
class TestMongoMemCellRepoAtomicApply:
    """``atomic_apply_strength_delta`` —— 含 scoped 租户隔离。"""

    async def test_increments_strength_and_access(self, repo: MongoMemCellRepo):
        cell = _cell(strength=1.0, access_count=2)
        await repo.insert(cell)
        applied = await repo.atomic_apply_strength_delta(
            cell.mem_cell_id,
            delta=0.3,
            s_max=5.0,
            increment_access=True,
        )
        assert applied == {"strength": pytest.approx(1.3), "access_count": 3}
        got = await repo.get_by_id(cell.mem_cell_id)
        assert got.strength == pytest.approx(1.3)
        assert got.access_count == 3

    async def test_clamps_at_s_max(self, repo: MongoMemCellRepo):
        cell = _cell(strength=4.9, access_count=0)
        await repo.insert(cell)
        applied = await repo.atomic_apply_strength_delta(
            cell.mem_cell_id,
            delta=0.5,
            s_max=5.0,
            increment_access=False,
        )
        assert applied == {"strength": 5.0, "access_count": 0}

    async def test_negative_delta_without_access_increment(self, repo: MongoMemCellRepo):
        cell = _cell(strength=2.0, access_count=5)
        await repo.insert(cell)
        applied = await repo.atomic_apply_strength_delta(
            cell.mem_cell_id,
            delta=-0.4,
            s_max=5.0,
            increment_access=False,
        )
        assert applied == {"strength": pytest.approx(1.6), "access_count": 5}
        got = await repo.get_by_id(cell.mem_cell_id)
        assert got.access_count == 5

    async def test_missing_cell_returns_none(self, repo: MongoMemCellRepo):
        applied = await repo.atomic_apply_strength_delta(
            "missing-id",
            delta=0.1,
            s_max=5.0,
            increment_access=True,
        )
        assert applied is None

    async def test_scoped_success(self, repo: MongoMemCellRepo):
        cell = _cell(strength=1.0, access_count=0)
        await repo.insert(cell)
        applied = await repo.atomic_apply_strength_delta(
            cell.mem_cell_id,
            delta=0.1,
            s_max=5.0,
            increment_access=True,
            tenant_id="t1",
            user_id="u1",
        )
        assert applied == {"strength": pytest.approx(1.1), "access_count": 1}

    async def test_scoped_rejects_wrong_tenant(self, repo: MongoMemCellRepo):
        cell = _cell(strength=1.0, access_count=0)
        await repo.insert(cell)
        applied = await repo.atomic_apply_strength_delta(
            cell.mem_cell_id,
            delta=0.1,
            s_max=5.0,
            increment_access=True,
            tenant_id="other-tenant",
            user_id="u1",
        )
        assert applied is None
        got = await repo.get_by_id(cell.mem_cell_id)
        assert got.strength == 1.0
        assert got.access_count == 0

    async def test_scoped_rejects_wrong_user(self, repo: MongoMemCellRepo):
        cell = _cell(strength=2.0, access_count=1)
        await repo.insert(cell)
        applied = await repo.atomic_apply_strength_delta(
            cell.mem_cell_id,
            delta=0.2,
            s_max=5.0,
            increment_access=True,
            tenant_id="t1",
            user_id="wrong-user",
        )
        assert applied is None
        got = await repo.get_by_id(cell.mem_cell_id)
        assert got.strength == 2.0
        assert got.access_count == 1

    async def test_partial_scope_rejected(self, repo: MongoMemCellRepo, caplog):
        """只传 tenant 时不做更新（fail-closed）。"""
        cell = _cell(strength=1.0, access_count=0)
        await repo.insert(cell)
        applied = await repo.atomic_apply_strength_delta(
            cell.mem_cell_id,
            delta=0.1,
            s_max=5.0,
            increment_access=False,
            tenant_id="t1",
        )
        assert applied is None
        got = await repo.get_by_id(cell.mem_cell_id)
        assert got.strength == 1.0
        assert "partial tenant scope rejected" in caplog.text

    async def test_sequential_deltas_accumulate(self, repo: MongoMemCellRepo):
        """两次 scoped 调用应基于落库值累加(模拟并发命中语义)。"""
        cell = _cell(strength=1.0, access_count=0)
        await repo.insert(cell)
        kw = {"tenant_id": "t1", "user_id": "u1", "s_max": 5.0, "increment_access": True}
        first = await repo.atomic_apply_strength_delta(cell.mem_cell_id, delta=0.1, **kw)
        second = await repo.atomic_apply_strength_delta(cell.mem_cell_id, delta=0.1, **kw)
        assert first == {"strength": pytest.approx(1.1), "access_count": 1}
        assert second == {"strength": pytest.approx(1.2), "access_count": 2}
