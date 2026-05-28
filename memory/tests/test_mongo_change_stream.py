"""MongoConfigCenter Change Stream 测试(管理面)。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- 没有 ``.watch`` 的 collection → 优雅降级,不启动 task
- 有 watch 时:启动后台 task,inserts → callback 收到 ConfigChangeEvent
- 过滤无关 op_type / 无 category 的 change
- 网络抖断后指数退避重连(单次失败 → 重连 → 继续消费)
- ``_db_stop_native_watch`` 干净退出(任务被取消 / event 设置)
- callback 抛异常不破坏主循环
- ``operationType=update`` 路径(updateLookup 才有 fullDocument)正确转换
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

import pytest

from memory_app.config_center import ConfigChangeEvent, MongoConfigCenter
from memory_app.config_center.mongo_center import (
    WATCH_BACKOFF_INITIAL_SECONDS,
)


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeChangeStream:
    """支持 ``async with`` + ``async for`` 的最小 Change Stream。"""

    def __init__(self, changes: Iterable[dict], raise_after: int | None = None):
        self._changes = list(changes)
        self._raise_after = raise_after
        self._closed = asyncio.Event()
        self._idx = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._closed.set()
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._raise_after is not None and self._idx >= self._raise_after:
            raise RuntimeError("network blip")
        if self._idx >= len(self._changes):
            # 没有更多事件 → 等到 close 被调
            await self._closed.wait()
            raise StopAsyncIteration
        change = self._changes[self._idx]
        self._idx += 1
        # 让出控制权,模拟流式
        await asyncio.sleep(0)
        return change


class _FakeCollWithWatch:
    """带 .watch 的 fake collection;每次调用返回**同一**预设 stream。"""

    def __init__(self, streams: list[_FakeChangeStream]):
        self._streams = list(streams)
        self.watch_calls = 0

    def watch(self, *args, **kwargs):
        self.watch_calls += 1
        if not self._streams:
            # 故意留一个空 stream 让循环进入等待
            return _FakeChangeStream([])
        return self._streams.pop(0)

    # Mongo 文档 CRUD 的最小必要方法,使得 _db_ensure_schema / 写入不抛
    async def create_index(self, *a, **kw):
        return None

    async def find_one(self, *a, **kw):
        return None

    async def update_one(self, *a, **kw):
        return None

    def find(self, *a, **kw):
        class _C:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            def sort(self, *a, **kw):
                return self

            def limit(self, *a, **kw):
                return self

        return _C()

    async def insert_one(self, *a, **kw):
        return None


class _FakeDBWithWatch:
    def __init__(self, coll: _FakeCollWithWatch):
        self._coll = coll

    def __getitem__(self, name: str):
        return self._coll

    async def command(self, name: str, *a, **kw):
        if name == "ping":
            return {"ok": 1}
        raise NotImplementedError(name)


class _FakeClientWithWatch:
    def __init__(self, coll: _FakeCollWithWatch):
        self._db = _FakeDBWithWatch(coll)

    def __getitem__(self, name: str):
        return self._db


def _build_center(coll: _FakeCollWithWatch) -> MongoConfigCenter:
    cli = _FakeClientWithWatch(coll)
    return MongoConfigCenter(cli, db_name="memory_test", defaults_flat={})


def _change(
    op: str,
    *,
    category: str | None = "memory.retrieval.fuser",
    name: str = "weighted_rrf",
    scope: str = "global",
    scope_id: str | None = None,
    version: int = 1,
    extra: dict | None = None,
) -> dict[str, Any]:
    """构造一个最小 Mongo Change Stream 文档。"""
    full = {
        "category": category,
        "name": name,
        "scope": scope,
        "scope_id": scope_id,
        "version": version,
        "actor": "ops",
    }
    if extra:
        full.update(extra)
    if category is None:
        full.pop("category")
    return {"operationType": op, "fullDocument": full}


# ════════════════════════════════════════════════════════════════════════════
# 测试
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestNoWatchAttribute:
    async def test_graceful_degrade_when_collection_lacks_watch(self):
        # 用 _FakeCollection（test_mongo_center_smoke.py 已验证可用）—— 它没有 .watch
        import memory_app.plugins_default  # noqa: F401  触发插件 @register
        from tests.test_mongo_center_smoke import _FakeClient

        cli = _FakeClient()
        cc = MongoConfigCenter(cli, db_name="memory_test", defaults_flat={})
        fired: list = []

        async def cb(e):
            fired.append(e)

        await cc.watch(cb)
        # 没启动 native task,仅 _native_watch_started=True
        assert cc._native_watch_started is True
        assert cc._watch_task is None
        # 即便没有 native stream,通过 write 走 _persist_entry 也能 _notify
        await cc.write("memory.retrieval.fuser", "noop_fuser", {"k": 70})
        assert any(e.category == "memory.retrieval.fuser" for e in fired)
        # 干净停
        await cc.close()


@pytest.mark.asyncio
class TestChangeStreamConsumption:
    async def test_inserts_become_events(self):
        stream = _FakeChangeStream(
            [
                _change("insert", version=1),
                _change("insert", version=2, name="bm25_es"),
            ]
        )
        coll = _FakeCollWithWatch([stream])
        cc = _build_center(coll)
        events: list[ConfigChangeEvent] = []

        async def cb(e):
            events.append(e)

        await cc.watch(cb)
        # 异步消费;给它最多 1 秒
        for _ in range(50):
            if len(events) >= 2:
                break
            await asyncio.sleep(0.01)

        assert len(events) >= 2
        assert events[0].category == "memory.retrieval.fuser"
        assert events[0].version == 1
        assert events[1].name == "bm25_es"
        await cc.close()

    async def test_filters_unrelated_change_ops_and_no_category(self):
        stream = _FakeChangeStream(
            [
                {"operationType": "delete", "fullDocument": None},  # 跳过
                _change("insert", category=None),  # 跳过(no category)
                _change("update", version=5),  # 收
            ]
        )
        coll = _FakeCollWithWatch([stream])
        cc = _build_center(coll)
        events: list = []

        async def cb(e):
            events.append(e)

        await cc.watch(cb)
        for _ in range(50):
            if events:
                break
            await asyncio.sleep(0.01)

        assert len(events) == 1
        assert events[0].version == 5
        await cc.close()

    async def test_callback_exception_is_swallowed(self):
        stream = _FakeChangeStream(
            [
                _change("insert", version=1),
                _change("insert", version=2),
                _change("insert", version=3),
            ]
        )
        coll = _FakeCollWithWatch([stream])
        cc = _build_center(coll)
        seen: list[int] = []
        bad: list[int] = []

        async def cb(e):
            if e.version == 2:
                bad.append(e.version)
                raise RuntimeError("user code crashed")
            seen.append(e.version)

        await cc.watch(cb)
        for _ in range(50):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.01)

        # version 1 与 3 都应进 seen;version 2 抛了被吞,不破坏主循环
        assert sorted(seen) == [1, 3]
        assert bad == [2]
        await cc.close()


@pytest.mark.asyncio
class TestReconnectOnTransientFailure:
    async def test_reconnects_after_stream_breaks(self, monkeypatch):
        # 第一次 stream 抛 RuntimeError;第二次 stream 正常
        broken = _FakeChangeStream([_change("insert", version=1)], raise_after=0)
        ok = _FakeChangeStream([_change("insert", version=2)])
        coll = _FakeCollWithWatch([broken, ok])
        cc = _build_center(coll)
        events: list = []

        async def cb(e):
            events.append(e)

        # 把退避缩短到 0,加速测试
        monkeypatch.setattr(
            "memory_app.config_center.mongo_center.WATCH_BACKOFF_INITIAL_SECONDS",
            0.01,
        )
        await cc.watch(cb)

        # 至少要让第二次 stream 被 watch_fn 调用
        for _ in range(200):
            if coll.watch_calls >= 2 and events:
                break
            await asyncio.sleep(0.01)

        assert coll.watch_calls >= 2
        assert any(e.version == 2 for e in events)
        await cc.close()


@pytest.mark.asyncio
class TestStopWatcherCleanly:
    async def test_close_cancels_change_stream_task(self):
        stream = _FakeChangeStream([_change("insert", version=1)])
        coll = _FakeCollWithWatch([stream])
        cc = _build_center(coll)

        async def cb(e):
            return None

        await cc.watch(cb)
        # 任务应在跑
        assert cc._watch_task is not None
        await cc.close()
        assert cc._watch_task is None
        assert cc._native_watch_started is False


# ════════════════════════════════════════════════════════════════════════════
# 静态:_change_to_event 边界
# ════════════════════════════════════════════════════════════════════════════
class TestChangeToEvent:
    def test_skips_delete(self):
        ev = MongoConfigCenter._change_to_event(
            {"operationType": "delete", "fullDocument": {"category": "x"}}
        )
        assert ev is None

    def test_skips_no_full_document(self):
        ev = MongoConfigCenter._change_to_event(
            {"operationType": "insert", "fullDocument": None}
        )
        assert ev is None

    def test_handles_missing_optional_fields(self):
        ev = MongoConfigCenter._change_to_event(
            {
                "operationType": "insert",
                "fullDocument": {"category": "memory.retrieval.fuser"},
            }
        )
        assert ev is not None
        assert ev.category == "memory.retrieval.fuser"
        assert ev.scope == "global"
        assert ev.version == 0
        assert ev.actor == "system"

    def test_handles_replace(self):
        ev = MongoConfigCenter._change_to_event(
            {
                "operationType": "replace",
                "fullDocument": {
                    "category": "x",
                    "scope": "tenant",
                    "scope_id": "t1",
                    "version": 7,
                    "name": "v2",
                    "actor": "alice",
                },
            }
        )
        assert ev is not None
        assert ev.scope == "tenant"
        assert ev.scope_id == "t1"
        assert ev.version == 7
        assert ev.name == "v2"
        assert ev.actor == "alice"
