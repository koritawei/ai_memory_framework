"""EntityStore 测试。"""

from __future__ import annotations

import pytest

from memory_app.entity_store import EntityStore, InMemoryEntityStore


# ════════════════════════════════════════════════════════════════════════════
# Fake Mongo
# ════════════════════════════════════════════════════════════════════════════
class _FakeCursor:
    def __init__(self, items):
        self._items = items

    async def to_list(self, length=None):
        if length is None:
            return list(self._items)
        return list(self._items)[:length]


class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []
        self.indexes: list[tuple] = []

    async def create_index(self, keys, **kw):
        self.indexes.append((tuple(keys), kw))

    async def update_one(self, filt, update, upsert=False):
        # 找到匹配 doc
        for d in self.docs:
            if all(d.get(k) == v for k, v in filt.items()):
                add = update.get("$addToSet", {})
                for k, v in add.items():
                    arr = d.setdefault(k, [])
                    if v not in arr:
                        arr.append(v)
                set_ = update.get("$set", {})
                for k, v in set_.items():
                    d[k] = v
                return type("R", (), {"modified_count": 1, "matched_count": 1})()
        if upsert:
            new = {}
            new.update(filt)
            for k, v in update.get("$setOnInsert", {}).items():
                new.setdefault(k, v)
            for k, v in update.get("$set", {}).items():
                new[k] = v
            for k, v in update.get("$addToSet", {}).items():
                new.setdefault(k, []).append(v)
            self.docs.append(new)
        return type("R", (), {"modified_count": 0, "matched_count": 0})()

    async def update_many(self, filt, update):
        modified = 0
        for d in self.docs:
            if all(d.get(k) == v for k, v in filt.items()):
                pull = update.get("$pull", {})
                for k, v in pull.items():
                    arr = d.get(k, [])
                    if v in arr:
                        arr.remove(v)
                        modified += 1
        return type("R", (), {"modified_count": modified})()

    def find(self, filt):
        match: list[dict] = []
        for d in self.docs:
            ok = True
            for k, v in filt.items():
                if isinstance(v, dict) and "$in" in v:
                    if d.get(k) not in v["$in"]:
                        ok = False
                        break
                else:
                    if d.get(k) != v:
                        ok = False
                        break
            if ok:
                match.append(d)
        return _FakeCursor(match)


class _FakeDB:
    def __init__(self):
        self._cols: dict[str, _FakeCollection] = {}

    def __getitem__(self, name):
        if name not in self._cols:
            self._cols[name] = _FakeCollection()
        return self._cols[name]


# ════════════════════════════════════════════════════════════════════════════
# EntityStore (Mongo 后端)
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestEntityStore:
    async def test_upsert_and_find(self):
        db = _FakeDB()
        store = EntityStore(db)
        n = await store.upsert_entities("mc1", ["北京", "出差"], "t1", "u1")
        assert n == 2
        await store.upsert_entities("mc2", ["北京", "旅游"], "t1", "u1")
        results = await store.find_by_entities(["北京"], "t1", "u1")
        assert "mc1" in results and "mc2" in results

    async def test_no_duplicate_mem_cell(self):
        db = _FakeDB()
        store = EntityStore(db)
        await store.upsert_entities("mc1", ["北京"], "t1", "u1")
        await store.upsert_entities("mc1", ["北京"], "t1", "u1")
        results = await store.find_by_entities(["北京"], "t1", "u1")
        # mc1 只出现一次
        assert results.count("mc1") == 1

    async def test_dedupe_input_entities(self):
        db = _FakeDB()
        store = EntityStore(db)
        n = await store.upsert_entities(
            "mc1", ["北京", "  ", "北京", "上海"], "t1", "u1"
        )
        assert n == 2  # "北京" 去重 + "" 过滤

    async def test_tenant_user_isolation(self):
        db = _FakeDB()
        store = EntityStore(db)
        await store.upsert_entities("mc1", ["北京"], "t1", "u1")
        await store.upsert_entities("mc2", ["北京"], "t1", "u2")  # 不同 user
        await store.upsert_entities("mc3", ["北京"], "t2", "u1")  # 不同 tenant
        u1_results = await store.find_by_entities(["北京"], "t1", "u1")
        assert u1_results == ["mc1"]

    async def test_multi_entity_query(self):
        db = _FakeDB()
        store = EntityStore(db)
        await store.upsert_entities("mc1", ["北京"], "t1", "u1")
        await store.upsert_entities("mc2", ["上海"], "t1", "u1")
        results = await store.find_by_entities(["北京", "上海"], "t1", "u1")
        assert set(results) == {"mc1", "mc2"}

    async def test_empty_entities_returns_empty(self):
        db = _FakeDB()
        store = EntityStore(db)
        assert await store.find_by_entities([], "t1", "u1") == []
        assert await store.upsert_entities("mc1", [], "t1", "u1") == 0

    async def test_remove_mem_cell(self):
        db = _FakeDB()
        store = EntityStore(db)
        await store.upsert_entities("mc1", ["北京", "上海"], "t1", "u1")
        affected = await store.remove_mem_cell("mc1", "t1", "u1")
        assert affected >= 1
        assert await store.find_by_entities(["北京"], "t1", "u1") == []

    async def test_ensure_indexes(self):
        db = _FakeDB()
        store = EntityStore(db)
        await store.ensure_indexes()
        coll = db["entities"]
        assert any(idx[1].get("unique") for idx in coll.indexes)


# ════════════════════════════════════════════════════════════════════════════
# InMemoryEntityStore
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestInMemoryEntityStore:
    async def test_basic_round_trip(self):
        s = InMemoryEntityStore()
        await s.upsert_entities("mc1", ["北京"], "t1", "u1")
        await s.upsert_entities("mc2", ["北京"], "t1", "u1")
        out = await s.find_by_entities(["北京"], "t1", "u1")
        assert set(out) == {"mc1", "mc2"}

    async def test_idempotent(self):
        s = InMemoryEntityStore()
        await s.upsert_entities("mc1", ["北京"], "t1", "u1")
        await s.upsert_entities("mc1", ["北京"], "t1", "u1")
        out = await s.find_by_entities(["北京"], "t1", "u1")
        assert out == ["mc1"]

    async def test_isolation(self):
        s = InMemoryEntityStore()
        await s.upsert_entities("mc1", ["x"], "t1", "u1")
        await s.upsert_entities("mc2", ["x"], "t1", "u2")
        assert await s.find_by_entities(["x"], "t1", "u1") == ["mc1"]
        assert await s.find_by_entities(["x"], "t1", "u2") == ["mc2"]

    async def test_remove(self):
        s = InMemoryEntityStore()
        await s.upsert_entities("mc1", ["a", "b"], "t1", "u1")
        n = await s.remove_mem_cell("mc1", "t1", "u1")
        assert n == 2
        assert await s.find_by_entities(["a"], "t1", "u1") == []
