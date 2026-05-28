"""pytest 公共固件。"""

from __future__ import annotations

import pytest

from memory_app.plugins.registry import registry as global_registry


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """每个测试用例前，确保关键环境变量未被宿主污染，并清掉 Settings 单例缓存。

    同时重置 PromptManager 全局单例 —— 否则前一个 test 触发的
    ``init_prompt_manager`` 会把 ConfigCenter 引用泄漏到下一个 test,
    新 test 拿到 *上一个* test 的 ConfigCenter,行为不可预测。
    """
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.settings import reset_settings_for_test

    for key in (
        "MEMORY_BOOTSTRAP_FILE",
        "MEMORY_MONGO_URI",
        "MEMORY_MONGO_DB",
        "MEMORY_AUTH_ENABLED",
        "MEMORY_CONFIG_CENTER_BACKEND",
        "MEMORY_CONFIG_CENTER_FILE_PATH",
        "MEMORY_STRICT_READINESS",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_settings_for_test()
    reset_prompt_manager_for_test()
    yield
    reset_settings_for_test()
    reset_prompt_manager_for_test()


@pytest.fixture
def fresh_registry(monkeypatch):
    """提供一个独立的 PluginRegistry 实例（避免互相污染）。

    用法：
        def test_xxx(fresh_registry):
            fresh_registry.register(MyPlugin)
            ...
    """
    from memory_app.plugins.registry import PluginRegistry

    fresh = PluginRegistry()
    return fresh


@pytest.fixture
def isolated_default_registry():
    """快照 + 还原全局 registry，避免测试之间互相污染。"""
    snapshot = {cat: dict(bucket) for cat, bucket in global_registry._plugins.items()}
    yield global_registry
    global_registry._plugins.clear()
    for cat, bucket in snapshot.items():
        global_registry._plugins[cat].update(bucket)
