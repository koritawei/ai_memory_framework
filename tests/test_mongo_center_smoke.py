"""MongoConfigCenter 烟测：不连真实 Mongo，仅用 motor 风格的 fake client
验证类层级（DBConfigCenter → MongoConfigCenter）正确。
"""

from __future__ import annotations

from typing import Any

import pytest

from memory_app.config_center import (
    BaseConfigCenter,
    DBConfigCenter,
    MongoConfigCenter,
)


class _FakeCursor:
    """支持 ``async for`` 与 ``.sort().limit()``。"""

    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def sort(self, *args, **kwargs):
        # 简化：sort 只关心 (field, direction) 列表的第一个 (field, dir)
        if args:
            spec = args[0]
            if isinstance(spec, list) and spec:
                field, direction = spec[0]
                self._docs.sort(key=lambda d: d.get(field, 0), reverse=(direction == -1))
        return self

    def limit(self, n: int):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeCollection:
    def __init__(self):
        self._rows: list[dict] = []
        self._indexes: list[Any] = []

    async def create_index(self, keys, **kwargs):
        self._indexes.append((keys, kwargs))
        return "ok"

    async def find_one(self, filt: dict):
        for d in self._rows:
            if all(d.get(k) == v for k, v in filt.items()):
                return dict(d)
        return None

    async def update_one(self, filt: dict, update: dict, upsert: bool = False):
        for d in self._rows:
            if all(d.get(k) == v for k, v in filt.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new = {**filt}
            new.update(update.get("$set", {}))
            self._rows.append(new)

    def find(self, filt: dict):
        matched = [d for d in self._rows if all(d.get(k) == v for k, v in filt.items())]
        return _FakeCursor(matched)

    async def insert_one(self, doc: dict):
        self._rows.append(dict(doc))


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._colls.setdefault(name, _FakeCollection())

    async def command(self, name: str, *args, **kwargs):
        if name == "ping":
            return {"ok": 1}
        raise NotImplementedError(name)


class _FakeClient:
    def __init__(self):
        self._dbs: dict[str, _FakeDB] = {}

    def __getitem__(self, name: str) -> _FakeDB:
        return self._dbs.setdefault(name, _FakeDB())


CATEGORY = "memory.retrieval.fuser"


@pytest.fixture
def mongo_center():
    import memory_app.plugins_default  # noqa: F401

    cli = _FakeClient()
    cc = MongoConfigCenter(
        cli,
        db_name="memory_test",
        defaults_flat={CATEGORY: {"name": "noop_fuser", "params": {"k": 60}}},
    )
    return cc


def test_class_hierarchy(mongo_center):
    """A + B 嵌套：MongoConfigCenter < DBConfigCenter < BaseConfigCenter < ConfigCenter。"""
    assert isinstance(mongo_center, MongoConfigCenter)
    assert isinstance(mongo_center, DBConfigCenter)
    assert isinstance(mongo_center, BaseConfigCenter)


@pytest.mark.asyncio
async def test_resolve_default_via_mongo(mongo_center: MongoConfigCenter):
    cfg = await mongo_center.resolve(CATEGORY)
    assert cfg.name == "noop_fuser"
    assert cfg.params["k"] == 60


@pytest.mark.asyncio
async def test_write_then_resolve_via_mongo(mongo_center: MongoConfigCenter):
    await mongo_center.write(CATEGORY, "noop_fuser", {"k": 80})
    cfg = await mongo_center.resolve(CATEGORY)
    assert cfg.params["k"] == 80
    assert cfg.source == "global"


@pytest.mark.asyncio
async def test_history_via_mongo(mongo_center: MongoConfigCenter):
    await mongo_center.write(CATEGORY, "noop_fuser", {"k": 70})
    await mongo_center.write(CATEGORY, "noop_fuser", {"k": 80})
    await mongo_center.write(CATEGORY, "noop_fuser", {"k": 90})
    hist = await mongo_center.history(CATEGORY, limit=10)
    assert len(hist) >= 2  # 第二、三次写入会把上一次的旧值进 history


@pytest.mark.asyncio
async def test_health_pings_db(mongo_center: MongoConfigCenter):
    h = await mongo_center.health()
    assert h["status"] == "ok"


@pytest.mark.asyncio
async def test_watch_ensures_schema(mongo_center: MongoConfigCenter):
    fired: list = []

    async def cb(event):
        fired.append(event)

    await mongo_center.watch(cb)
    # 写一次触发 callback
    await mongo_center.write(CATEGORY, "noop_fuser", {"k": 100})
    assert any(e.category == CATEGORY for e in fired)
    # 索引被创建过
    coll = mongo_center._coll
    assert len(coll._indexes) >= 1
