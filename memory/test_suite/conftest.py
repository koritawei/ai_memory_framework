"""test_suite 共用 fixture。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def project_cwd(project_root: Path):
    cwd = os.getcwd()
    os.chdir(project_root)
    try:
        yield project_root
    finally:
        os.chdir(cwd)


@pytest.fixture
def isolated_default_yaml(tmp_path: Path, monkeypatch) -> Path:
    import shutil

    src = PROJECT_ROOT / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    shutil.copy2(src, dst)
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    return dst


@pytest.fixture(autouse=True)
def _ensure_plugins_loaded():
    import memory_app.plugins_default  # noqa: F401


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: 测试方案端到端流程用例")
    config.addinivalue_line("markers", "gate: CI 门禁（审计等）")
    config.addinivalue_line("markers", "nft: 非功能烟测（性能等）")


@pytest.fixture
def api_client(project_cwd, isolated_default_yaml):
    """无业务注入的裸 TestClient（健康、Admin、Schema 校验）。"""
    from memory_app import api
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.settings import reset_settings_for_test

    reset_settings_for_test()
    reset_prompt_manager_for_test()
    with TestClient(api.app) as client:
        yield client
    reset_prompt_manager_for_test()
