"""BaseConfigCenter 契约测试。

通过一个**纯内存的 fake 子类**验证：任何后端只要正确实现 4 个 hook，
通用流程（resolve / write / history / watch / version 自增 / schema 校验）就一定正确。

未来 etcd / Apollo / 自研后端可以直接复用本测试套件。
"""

from __future__ import annotations

import asyncio
import copy
from typing import Awaitable, Callable, Optional

import pytest

from memory_app.config_center import BaseConfigCenter, ConfigValidationError
from memory_app.config_center.base import ConfigChangeEvent


class _InMemBackend(BaseConfigCenter):
    """最小可用的内存后端，仅用于契约测试。"""

    def __init__(self, *, defaults_flat: dict | None = None) -> None:
        super().__init__(defaults_flat=defaults_flat or {})
        self._global: dict = {}
        self._tenant: dict = {}
        self._user: dict = {}
        self._history: list[dict] = []
        self._watch_running = False
        self._on_event: Callable | None = None

    async def _load_overrides(self) -> tuple[dict, dict, dict]:
        return (
            copy.deepcopy(self._global),
            copy.deepcopy(self._tenant),
            copy.deepcopy(self._user),
        )

    async def _persist_entry(
        self,
        *,
        category: str,
        scope: str,
        scope_id: Optional[str],
        entry: dict,
        actor: str,
    ) -> int:
        if scope == "global":
            self._global[category] = entry
        elif scope == "tenant":
            self._tenant.setdefault(scope_id, {})[category] = entry
        elif scope == "user":
            self._user.setdefault(scope_id, {})[category] = entry
        new_v = self._version + 1
        self._history.insert(
            0,
            {
                "category": category,
                "scope": scope,
                "scope_id": scope_id,
                "name": entry["name"],
                "params": entry.get("params", {}),
                "version": new_v,
                "actor": actor,
            },
        )
        return new_v

    async def _read_history(self, category: str, limit: int) -> list[dict]:
        return [h for h in self._history if h["category"] == category][:limit]

    async def _spawn_watcher(
        self, on_native_event: Callable[[ConfigChangeEvent], Awaitable[None]]
    ) -> None:
        self._watch_running = True
        self._on_event = on_native_event

    async def _stop_watcher(self) -> None:
        self._watch_running = False
        self._on_event = None

    # 测试辅助：模拟外部变更
    async def _simulate_external_change(self, category: str) -> None:
        if self._on_event:
            await self._on_event(
                ConfigChangeEvent(category=category, version=self._version + 1, actor="test")
            )


# ─────────────────────────── 契约测试 ───────────────────────────
CATEGORY = "memory.retrieval.fuser"


@pytest.fixture
def backend():
    # 触发默认插件注册（noop_fuser 提供 schema）
    import memory_app.plugins_default  # noqa: F401

    return _InMemBackend(
        defaults_flat={
            CATEGORY: {"name": "noop_fuser", "params": {"k": 60}},
        }
    )


@pytest.mark.asyncio
async def test_resolve_default(backend: _InMemBackend):
    cfg = await backend.resolve(CATEGORY)
    assert cfg.name == "noop_fuser"
    assert cfg.params["k"] == 60
    assert cfg.source == "default"


@pytest.mark.asyncio
async def test_write_then_resolve(backend: _InMemBackend):
    new_v = await backend.write(CATEGORY, "noop_fuser", {"k": 80})
    assert new_v >= 1
    cfg = await backend.resolve(CATEGORY)
    assert cfg.params["k"] == 80
    assert cfg.source == "global"


@pytest.mark.asyncio
async def test_write_validates_schema(backend: _InMemBackend):
    # noop_fuser 的 k ∈ [1, 1000]；越界应直接拒绝写入
    with pytest.raises(ConfigValidationError):
        await backend.write(CATEGORY, "noop_fuser", {"k": 99999})


@pytest.mark.asyncio
async def test_write_unknown_plugin(backend: _InMemBackend):
    with pytest.raises(ConfigValidationError):
        await backend.write(CATEGORY, "no_such_plugin", {"k": 60})


@pytest.mark.asyncio
async def test_write_invalid_scope(backend: _InMemBackend):
    with pytest.raises(ValueError):
        await backend.write(CATEGORY, "noop_fuser", {"k": 60}, scope="invalid")


@pytest.mark.asyncio
async def test_write_tenant_requires_scope_id(backend: _InMemBackend):
    with pytest.raises(ValueError):
        await backend.write(CATEGORY, "noop_fuser", {"k": 60}, scope="tenant")


@pytest.mark.asyncio
async def test_history_after_writes(backend: _InMemBackend):
    await backend.write(CATEGORY, "noop_fuser", {"k": 70})
    await backend.write(CATEGORY, "noop_fuser", {"k": 80})
    hist = await backend.history(CATEGORY, limit=10)
    assert len(hist) >= 2
    assert hist[0]["params"]["k"] == 80


@pytest.mark.asyncio
async def test_watch_callback_fires(backend: _InMemBackend):
    received: list = []

    async def cb(event):
        received.append(event)

    await backend.watch(cb)
    await backend.write(CATEGORY, "noop_fuser", {"k": 100})
    assert len(received) >= 1
    assert received[-1].category == CATEGORY


@pytest.mark.asyncio
async def test_external_change_triggers_callbacks(backend: _InMemBackend):
    received: list = []

    async def cb(event):
        received.append(event)

    await backend.watch(cb)
    await backend._simulate_external_change(CATEGORY)
    assert any(e.actor == "test" for e in received)


@pytest.mark.asyncio
async def test_callback_failure_does_not_abort(backend: _InMemBackend):
    successes: list = []

    async def bad_cb(event):
        raise RuntimeError("boom")

    async def good_cb(event):
        successes.append(event)

    await backend.watch(bad_cb)
    await backend.watch(good_cb)
    await backend.write(CATEGORY, "noop_fuser", {"k": 100})
    assert len(successes) >= 1


@pytest.mark.asyncio
async def test_tenant_overrides_take_priority(backend: _InMemBackend):
    await backend.write(CATEGORY, "noop_fuser", {"k": 80}, scope="global")
    await backend.write(CATEGORY, "noop_fuser", {"k": 200}, scope="tenant", scope_id="acme")
    cfg = await backend.resolve(CATEGORY, tenant_id="acme")
    assert cfg.params["k"] == 200
    assert cfg.source == "tenant"
    cfg2 = await backend.resolve(CATEGORY, tenant_id="other_corp")
    assert cfg2.params["k"] == 80
    assert cfg2.source == "global"


@pytest.mark.asyncio
async def test_close_stops_watcher(backend: _InMemBackend):
    async def cb(event):
        pass

    await backend.watch(cb)
    assert backend._watch_running is True
    await backend.close()
    assert backend._watch_running is False


@pytest.mark.asyncio
async def test_version_monotonic(backend: _InMemBackend):
    v0 = (await backend.resolve(CATEGORY)).version
    v1 = await backend.write(CATEGORY, "noop_fuser", {"k": 70})
    v2 = await backend.write(CATEGORY, "noop_fuser", {"k": 80})
    assert v0 <= v1 < v2
