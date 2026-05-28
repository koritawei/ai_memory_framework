""" 验收：PluginRegistry 行为。"""

from __future__ import annotations

import pytest

from memory_app.plugins import (
    Plugin,
    PluginConflictError,
    PluginMeta,
    PluginNotFoundError,
)
from memory_app.plugins.registry import PluginRegistry


class _DummyA(Plugin):
    meta = PluginMeta(name="dummy", category="test_category", version="0.0.1")

    async def start(self, config):  # pragma: no cover - trivial
        pass

    async def stop(self):  # pragma: no cover - trivial
        pass


class _DummyB(Plugin):
    meta = PluginMeta(name="dummy", category="test_category", version="0.0.2")

    async def start(self, config):  # pragma: no cover - trivial
        pass

    async def stop(self):  # pragma: no cover - trivial
        pass


def test_register_and_get(fresh_registry: PluginRegistry):
    fresh_registry.register(_DummyA)
    cls = fresh_registry.get("test_category", "dummy")
    assert cls is _DummyA


def test_register_idempotent_same_class(fresh_registry: PluginRegistry):
    fresh_registry.register(_DummyA)
    # 再次注册同一个类应静默通过（多次 import 场景）
    fresh_registry.register(_DummyA)
    assert fresh_registry.get("test_category", "dummy") is _DummyA


def test_register_conflict(fresh_registry: PluginRegistry):
    fresh_registry.register(_DummyA)
    with pytest.raises(PluginConflictError):
        fresh_registry.register(_DummyB)


def test_get_missing_raises_lookup(fresh_registry: PluginRegistry):
    # PluginNotFoundError 同时是 LookupError，便于第三方代码常规捕获
    with pytest.raises(LookupError):
        fresh_registry.get("missing_cat", "missing_name")
    with pytest.raises(PluginNotFoundError):
        fresh_registry.get("missing_cat", "missing_name")


def test_list_by_category(fresh_registry: PluginRegistry):
    class _Other(Plugin):
        meta = PluginMeta(name="other", category="other_cat", version="0.0.1")

        async def start(self, config):
            pass

        async def stop(self):
            pass

    fresh_registry.register(_DummyA)
    fresh_registry.register(_Other)
    assert {c.meta.name for c in fresh_registry.list("test_category")} == {"dummy"}
    assert {c.meta.category for c in fresh_registry.list()} == {"test_category", "other_cat"}


def test_describe_returns_summary(fresh_registry: PluginRegistry):
    fresh_registry.register(_DummyA)
    desc = fresh_registry.describe()
    assert "test_category" in desc
    assert desc["test_category"][0]["name"] == "dummy"
    assert desc["test_category"][0]["version"] == "0.0.1"


def test_register_class_without_meta_raises(fresh_registry: PluginRegistry):
    class NoMeta(Plugin):
        # 故意不声明 meta
        async def start(self, config):
            pass

        async def stop(self):
            pass

    with pytest.raises(TypeError):
        fresh_registry.register(NoMeta)


def test_discover_entry_points_does_not_raise(fresh_registry: PluginRegistry):
    # 实际项目里可能没有第三方 entry_points；不抛异常即可
    n = fresh_registry.discover_entry_points("nonexistent.group.42")
    assert isinstance(n, int) and n >= 0
