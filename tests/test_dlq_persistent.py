"""持久化 DLQ 测试。"""

from __future__ import annotations

import pytest

from memory_app.repositories.dlq import DLQRecord, InMemoryDLQ
from memory_app.repositories.mongo_dlq import MongoDLQ
from memory_app.repositories.redis_dlq import RedisDLQ


class _FakeMongoCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    def find(self, filt=None):
        items = list(self.docs)
        if filt and "target" in filt:
            items = [d for d in items if d.get("target") == filt["target"]]

        class _Cursor:
            def __init__(self, data):
                self._data = sorted(
                    data, key=lambda d: d.get("timestamp", ""), reverse=True
                )

            def sort(self, *_a, **_k):
                return self

            def limit(self, n):
                self._data = self._data[:n]
                return self

            async def to_list(self, length=50):
                return self._data[:length]

        return _Cursor(items)

    async def create_index(self, *_a, **_k):
        return None

    async def count_documents(self, _filt):
        return len(self.docs)


class _FakeMongoDb:
    def __init__(self):
        self._coll = _FakeMongoCollection()

    def __getitem__(self, name):
        return self._coll


class _FakeRedis:
    def __init__(self):
        self._lists: dict[str, list[str]] = {}

    async def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self._lists[key] = self._lists.get(key, [])[start : end + 1]

    async def lrange(self, key, start, end):
        return self._lists.get(key, [])[start : end + 1]

    async def llen(self, key):
        return len(self._lists.get(key, []))


@pytest.mark.asyncio
async def test_mongo_dlq_persists_records():
    dlq = MongoDLQ(_FakeMongoDb())
    rec = DLQRecord(target="es", mem_cell_id="mc1", error="timeout")
    await dlq.enqueue(rec)
    items = await dlq.list(limit=10)
    assert len(items) == 1
    assert items[0].mem_cell_id == "mc1"


@pytest.mark.asyncio
async def test_redis_dlq_persists_records():
    dlq = RedisDLQ(_FakeRedis(), key="test:dlq")
    rec = DLQRecord(target="milvus", mem_cell_id="mc2", error="down")
    await dlq.enqueue(rec)
    items = await dlq.list(limit=10)
    assert len(items) == 1
    assert items[0].target == "milvus"


@pytest.mark.asyncio
async def test_in_memory_dlq_still_works():
    dlq = InMemoryDLQ(max_size=10)
    await dlq.enqueue(DLQRecord(target="es", mem_cell_id="x", error="e"))
    assert await dlq.size() == 1
