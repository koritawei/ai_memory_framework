""" 验收：health 端点。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def project_cwd():
    cwd = os.getcwd()
    os.chdir(Path(__file__).resolve().parent.parent)
    try:
        yield
    finally:
        os.chdir(cwd)


@pytest.fixture
def client(project_cwd, monkeypatch):
    # 默认 file backend；外部依赖不可达不会阻塞启动
    from memory_app import api
    from memory_app.settings import reset_settings_for_test

    reset_settings_for_test()
    with TestClient(api.app) as c:
        yield c


def test_liveness(client: TestClient):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_returns_known_checks(client: TestClient):
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded", "fail")
    checks = body["checks"]
    for key in ("mongo", "es", "redis", "milvus", "config_center", "plugin_registry"):
        assert key in checks, f"missing check: {key}"
    # 必启项必须 ok
    assert checks["plugin_registry"]["status"] == "ok"
    assert checks["config_center"]["status"] in ("ok", "degraded")


def test_admin_plugins_lists_categories(client: TestClient):
    r = client.get("/v1/admin/plugins")
    assert r.status_code == 200
    body = r.json()
    cats = body["categories"]
    assert "memory.generation.boundary_detector" in cats
    assert "memory.retrieval.fuser" in cats


def test_openapi_title(client: TestClient):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"] == "Memory Service"
