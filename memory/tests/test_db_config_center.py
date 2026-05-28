"""DBConfigCenter 契约测试。

通过一个**纯内存的 fake DB 子类**验证：任何 DB 后端只要正确实现 9 个 ``_db_*``
原语，DBConfigCenter 的通用 CRUD 流程（含 TTL 缓存、version 自增、history、
schema 自动建立）就一定正确。

将来 :class:`MongoConfigCenter` / 未来的 ``PGConfigCenter`` /
``SQLiteConfigCenter`` 应能完整复用本测试套件。
"""

from __future__ import annotations

import asyncio
import copy
from typing import Awaitable, Callable, Optional

import pytest

from memory_app.config_center import DBConfigCenter
from memory_app.config_center.base import ConfigChangeEvent


class _InMemDB(DBConfigCenter):
    """最小可用的内存 DB 后端。"""

    overrides_cache_ttl_seconds = 0.0  # 禁用缓存，测试每次读 DB

    def __init__(self, *, defaults_flat: dict | None = None) -> None:
        super().__init__(defaults_flat=defaults_flat or {})
        self._table: dict[tuple, dict] = {}        # (category, scope, scope_id) → doc
        self._hist: list[dict] = []
        self._schema_ensured = False
        self._native_started = False
        self._on_event_holder: Callable | None = None

    # ── 9 原语实现 ──
    async def _db_ensure_schema(self) -> None:
        self._schema_ensured = True

    async def _db_find_entry(self, *, category, scope, scope_id):
        return copy.deepcopy(self._table.get((category, scope, scope_id)))

    async def _db_upsert_entry(self, doc):
        key = (doc["category"], doc["scope"], doc["scope_id"])
        self._table[key] = copy.deepcopy(doc)

    async def _db_query_overrides_by_scope(self, scope):
        out: dict = {}
        for (cat, sc, sid), d in self._table.items():
            if sc != scope:
                continue
            entry = {"name": d["name"], "params": d.get("params", {}) or {}}
            if d.get("variants"):
                entry["variants"] = d["variants"]
            if scope == "global":
                out[cat] = entry
            else:
                out.setdefault(sid, {})[cat] = entry
        return out

    async def _db_insert_history(self, doc):
        self._hist.insert(0, copy.deepcopy(doc))

    async def _db_query_history(self, category, limit):
        return [h for h in self._hist if h.get("category") == category][:limit]

    async def _db_start_native_watch(self, on_event):
        self._native_started = True
        self._on_event_holder = on_event

    async def _db_stop_native_watch(self):
        self._native_started = False
        self._on_event_holder = None

    async def _db_ping(self) -> bool:
        return True

    # 测试辅助
    async def _simulate_native_change(self, category: str) -> None:
        if self._on_event_holder:
            await self._on_event_holder(
                ConfigChangeEvent(category=category, version=0, actor="db_native")
            )


# ─────────────────────────── 契约测试 ───────────────────────────
CATEGORY = "memory.retrieval.fuser"


@pytest.fixture
def db():
    import memory_app.plugins_default  # noqa: F401

    return _InMemDB(
        defaults_flat={CATEGORY: {"name": "noop_fuser", "params": {"k": 60}}}
    )


@pytest.mark.asyncio
async def test_resolve_default(db: _InMemDB):
    cfg = await db.resolve(CATEGORY)
    assert cfg.name == "noop_fuser"
    assert cfg.params["k"] == 60


@pytest.mark.asyncio
async def test_write_then_resolve_with_per_entry_version(db: _InMemDB):
    v1 = await db.write(CATEGORY, "noop_fuser", {"k": 80})
    cfg = await db.resolve(CATEGORY)
    assert cfg.params["k"] == 80
    # 第二次写入同一 entry，DB 内部 version 应递增到 2
    v2 = await db.write(CATEGORY, "noop_fuser", {"k": 100})
    entry = await db._db_find_entry(category=CATEGORY, scope="global", scope_id=None)
    assert entry["version"] == 2
    assert v2 > v1


@pytest.mark.asyncio
async def test_history_records_old_value(db: _InMemDB):
    await db.write(CATEGORY, "noop_fuser", {"k": 70})
    await db.write(CATEGORY, "noop_fuser", {"k": 80})
    await db.write(CATEGORY, "noop_fuser", {"k": 90})
    hist = await db.history(CATEGORY, limit=10)
    # 第二、三次写入会把上一次的旧值进 history
    assert len(hist) >= 2
    # history 中各版本递增（从大到小）
    versions = [h["version"] for h in hist]
    assert versions == sorted(versions, reverse=True)


@pytest.mark.asyncio
async def test_ensure_schema_called_on_watch(db: _InMemDB):
    assert db._schema_ensured is False

    async def cb(event):
        pass

    await db.watch(cb)
    assert db._schema_ensured is True
    assert db._native_started is True


@pytest.mark.asyncio
async def test_native_event_invalidates_cache(db: _InMemDB):
    db.overrides_cache_ttl_seconds = 60  # 启用长 TTL，确认 watcher 可以主动失效

    async def cb(event):
        pass

    await db.watch(cb)
    # 第一次 resolve → 缓存填充
    await db.resolve(CATEGORY)
    assert db._overrides_cache is not None
    # 模拟外部 DB 变更
    await db._simulate_native_change(CATEGORY)
    assert db._overrides_cache is None  # 缓存被失效


@pytest.mark.asyncio
async def test_health_via_ping(db: _InMemDB):
    h = await db.health()
    assert h["status"] == "ok"


@pytest.mark.asyncio
async def test_invalidate_cache_manual(db: _InMemDB):
    db.overrides_cache_ttl_seconds = 60
    await db.resolve(CATEGORY)
    assert db._overrides_cache is not None
    db.invalidate_cache()
    assert db._overrides_cache is None


@pytest.mark.asyncio
async def test_close_stops_native_watch(db: _InMemDB):
    async def cb(event):
        pass

    await db.watch(cb)
    assert db._native_started is True
    await db.close()
    assert db._native_started is False


@pytest.mark.asyncio
async def test_overrides_cascade_through_db(db: _InMemDB):
    await db.write(CATEGORY, "noop_fuser", {"k": 80}, scope="global")
    await db.write(CATEGORY, "noop_fuser", {"k": 200}, scope="tenant", scope_id="acme")
    await db.write(CATEGORY, "noop_fuser", {"k": 300}, scope="user", scope_id="u1")

    g = await db.resolve(CATEGORY)
    assert g.params["k"] == 80
    t = await db.resolve(CATEGORY, tenant_id="acme")
    assert t.params["k"] == 200
    u = await db.resolve(CATEGORY, tenant_id="acme", user_id="u1")
    assert u.params["k"] == 300
