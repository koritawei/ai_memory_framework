"""FileConfigCenter 端到端：读取、解析、写回、watch。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from memory_app.config_center import FileConfigCenter


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    p = tmp_path / "default.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "defaults": {
                    "memory": {
                        "retrieval": {
                            "fuser": {"name": "noop_fuser", "params": {"k": 60}}
                        }
                    }
                },
                "global_overrides": {},
                "tenant_overrides": {},
                "user_overrides": {},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return p


@pytest.mark.asyncio
async def test_resolve_default(yaml_path: Path):
    # 触发默认插件注册（让 schema 校验生效）
    import memory_app.plugins_default  # noqa: F401

    cc = FileConfigCenter(yaml_path)
    cfg = await cc.resolve("memory.retrieval.fuser")
    assert cfg.name == "noop_fuser"
    assert cfg.params["k"] == 60
    assert cfg.source == "default"
    await cc.close()


@pytest.mark.asyncio
async def test_write_then_resolve(yaml_path: Path):
    import memory_app.plugins_default  # noqa: F401

    cc = FileConfigCenter(yaml_path)

    received: list = []

    async def cb(event):
        received.append(event)

    await cc.watch(cb)

    new_v = await cc.write(
        "memory.retrieval.fuser",
        "noop_fuser",
        {"k": 80},
        scope="global",
        actor="test",
    )
    assert new_v >= 1

    cfg = await cc.resolve("memory.retrieval.fuser")
    assert cfg.params["k"] == 80
    assert cfg.source == "global"

    # callback 应至少被调用 1 次
    assert len(received) >= 1
    await cc.close()


@pytest.mark.asyncio
async def test_history(yaml_path: Path):
    import memory_app.plugins_default  # noqa: F401

    cc = FileConfigCenter(yaml_path)
    await cc.write("memory.retrieval.fuser", "noop_fuser", {"k": 80}, scope="global")
    await cc.write("memory.retrieval.fuser", "noop_fuser", {"k": 100}, scope="global")
    hist = await cc.history("memory.retrieval.fuser", limit=5)
    assert len(hist) >= 2
    assert hist[0]["params"]["k"] == 100
    await cc.close()


@pytest.mark.asyncio
async def test_health(yaml_path: Path):
    cc = FileConfigCenter(yaml_path)
    h = await cc.health()
    assert h["status"] == "ok"
    await cc.close()


@pytest.mark.asyncio
async def test_health_when_file_missing(tmp_path: Path):
    cc = FileConfigCenter(tmp_path / "missing.yaml")
    h = await cc.health()
    assert h["status"] == "degraded"
    await cc.close()
