"""MongoMemCellRepo 测试。"""

from __future__ import annotations

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
        return dict(self.docs[mid]) if mid in self.docs else None

    async def update_one(self, filt, update):
        mid = filt.get("mem_cell_id")
        if mid not in self.docs:
            return _FakeResult(modified=0)
        self.docs[mid].update(update.get("$set", {}))
        return _FakeResult(modified=1)

    async def delete_one(self, filt):
        mid = filt.get("mem_cell_id")
        if mid in self.docs:
            del self.docs[mid]
            return _FakeResult(deleted=1)
        return _FakeResult(deleted=0)


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
