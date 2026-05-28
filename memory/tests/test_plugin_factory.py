""" + 0.6 联调：PluginFactory + FileConfigCenter 通路。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memory_app.config_center import FileConfigCenter
from memory_app.plugins import PluginFactory


@pytest.fixture
def project_root() -> Path:
    """项目根目录（pyproject.toml 所在）。"""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def default_config_path(project_root: Path) -> Path:
    return project_root / "config" / "default.yaml"


@pytest.fixture
def project_cwd(project_root: Path):
    """切换到项目根目录，避免 FileConfigCenter 相对路径错位。"""
    cwd = os.getcwd()
    os.chdir(project_root)
    try:
        yield project_root
    finally:
        os.chdir(cwd)


@pytest.fixture
def factory(default_config_path: Path, project_cwd):
    # 触发默认插件注册
    import memory_app.plugins_default  # noqa: F401
    from memory_app.plugins import registry

    cc = FileConfigCenter(default_config_path)
    return PluginFactory(registry, cc)


@pytest.mark.asyncio
async def test_factory_builds_default_sbd(factory: PluginFactory):
    """写入热路径 起 default.yaml 切到 rule_sbd;noop_sbd 仍可通过显式覆盖访问。"""
    sbd = await factory.build("memory.generation.boundary_detector")
    assert sbd.meta.name == "rule_sbd"
    health = await sbd.health()
    assert health["status"] == "ok"


@pytest.mark.asyncio
async def test_factory_builds_default_fuser(factory: PluginFactory):
    """检索 起 default.yaml 切到 weighted_rrf;noop_fuser 仍可通过显式覆盖访问。"""
    fuser = await factory.build("memory.retrieval.fuser")
    assert fuser.meta.name == "weighted_rrf"
    health = await fuser.health()
    assert health["status"] == "ok"


@pytest.mark.asyncio
async def test_factory_caches_instance(factory: PluginFactory):
    a = await factory.build("memory.retrieval.fuser")
    b = await factory.build("memory.retrieval.fuser")
    assert a is b


@pytest.mark.asyncio
async def test_factory_unknown_category_raises(factory: PluginFactory):
    from memory_app.config_center.base import ConfigValidationError

    with pytest.raises((LookupError, ConfigValidationError, Exception)):
        await factory.build("definitely.not.a.real.category")


# ════════════════════════════════════════════════════════════════════════════
# P3.1 per-key 锁:并发 + 失败路径不泄漏 lock
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_factory_per_key_lock_allows_concurrent_build():
    """两个不同 category 的 build 应并发执行,不互相等待。"""
    import asyncio
    import time

    from memory_app.config_center.base import ResolvedPluginConfig
    from memory_app.plugins import PluginRegistry
    from memory_app.plugins.base import Plugin, PluginMeta

    class _SlowA(Plugin):
        meta = PluginMeta(name="sa", category="test.par.a", version="1.0.0")

        async def start(self, config):
            await asyncio.sleep(0.05)

        async def stop(self):
            return None

    class _SlowB(Plugin):
        meta = PluginMeta(name="sb", category="test.par.b", version="1.0.0")

        async def start(self, config):
            await asyncio.sleep(0.05)

        async def stop(self):
            return None

    reg = PluginRegistry()
    reg.register(_SlowA)
    reg.register(_SlowB)

    class _Cfg:
        async def resolve(self, cat, **kw):
            name = "sa" if cat.endswith(".a") else "sb"
            return ResolvedPluginConfig(name=name, params={}, version=1)

        async def watch(self, cb):
            return None

    fac = PluginFactory(reg, _Cfg())
    t0 = time.perf_counter()
    await asyncio.gather(
        fac.build("test.par.a"),
        fac.build("test.par.b"),
    )
    elapsed = time.perf_counter() - t0
    # 串行需 ~0.1s,并发应 ~0.05s + 调度开销;保守判定 < 0.09s
    assert elapsed < 0.09, f"per-key lock 未生效,耗时 {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_factory_start_failure_does_not_leak_lock():
    """P3.1 bugfix:start 抛错时 _build_locks 应被清理,避免内存泄漏。"""
    from memory_app.config_center.base import ResolvedPluginConfig
    from memory_app.plugins import PluginRegistry
    from memory_app.plugins.base import Plugin, PluginError, PluginMeta

    class _Broken(Plugin):
        meta = PluginMeta(name="broken", category="test.broken", version="1.0.0")

        async def start(self, config):
            raise RuntimeError("simulated start failure")

        async def stop(self):
            return None

    reg = PluginRegistry()
    reg.register(_Broken)

    class _Cfg:
        async def resolve(self, cat, **kw):
            return ResolvedPluginConfig(name="broken", params={}, version=1)

        async def watch(self, cb):
            return None

    fac = PluginFactory(reg, _Cfg())
    # 连续多次失败,每次都应清理 lock
    for _ in range(3):
        with pytest.raises(PluginError):
            await fac.build("test.broken")
    # 关键断言:_build_locks 不应残留失败 build 的 lock
    assert ("test.broken", "broken", "*", 1) not in fac._build_locks, (
        "失败路径泄漏了 lock 引用,_build_locks 字典会无限增长"
    )
