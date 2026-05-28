"""POST /v1/memory/consolidate 端点测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_app.deps import app_state
from memory_app.plugins.spi.consolidation_strategy import ConsolidationReport
from memory_app.services import ConsolidationService


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _StubStrategy:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    async def run(self, scope="all", time=None):
        self.calls.append((scope, time))
        if self.fail:
            raise RuntimeError("strategy failed")
        now = datetime.now(timezone.utc)
        return ConsolidationReport(
            phase="light" if scope == "all" else scope,
            started_at=now,
            finished_at=now,
            scanned_count=10,
            consolidated_count=2,
            archived_count=1,
        )


@pytest.fixture(autouse=True)
def _ensure_plugins_loaded():
    import memory_app.plugins_default  # noqa: F401


@pytest.fixture
def project_cwd():
    cwd = os.getcwd()
    os.chdir(Path(__file__).resolve().parent.parent)
    try:
        yield
    finally:
        os.chdir(cwd)


@pytest.fixture
def isolated_default_yaml(tmp_path: Path, monkeypatch) -> Path:
    import shutil

    src = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    shutil.copy2(src, dst)
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    return dst


# ════════════════════════════════════════════════════════════════════════════
# Happy path
# ════════════════════════════════════════════════════════════════════════════
class TestConsolidateEndpoint:
    @pytest.fixture
    def client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            app_state.consolidation_service = ConsolidationService(strategy=_StubStrategy())
            yield c
        reset_prompt_manager_for_test()

    def test_happy_path(self, client):
        body = {"tenant_id": "t1", "user_id": "u1"}
        r = client.post("/v1/memory/consolidate", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "ok"
        assert "phase" in data
        assert data["scanned_count"] == 10
        assert data["consolidated_count"] == 2
        assert data["archived_count"] == 1

    def test_explicit_scope(self, client):
        body = {"tenant_id": "t1", "user_id": "u1", "scope": "deep"}
        r = client.post("/v1/memory/consolidate", json=body)
        assert r.status_code == 200
        assert r.json()["phase"] == "deep"

    def test_user_id_optional(self, client):
        body = {"tenant_id": "t1"}
        r = client.post("/v1/memory/consolidate", json=body)
        assert r.status_code == 200

    def test_dry_run_passthrough(self, client):
        body = {"tenant_id": "t1", "user_id": "u1", "dry_run": True}
        r = client.post("/v1/memory/consolidate", json=body)
        assert r.status_code == 200
        assert r.json()["dry_run"] is True

    def test_missing_tenant_returns_422(self, client):
        r = client.post("/v1/memory/consolidate", json={})
        assert r.status_code == 422


# ════════════════════════════════════════════════════════════════════════════
# 服务未就绪 → 200 + not_implemented
# ════════════════════════════════════════════════════════════════════════════
class TestConsolidateNotImplemented:
    @pytest.fixture
    def client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            app_state.consolidation_service = None
            yield c
        reset_prompt_manager_for_test()

    def test_not_implemented_returns_200(self, client):
        body = {"tenant_id": "t1", "user_id": "u1"}
        r = client.post("/v1/memory/consolidate", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "not_implemented"
        assert data["detail"]["code"] == "not_implemented"


# ════════════════════════════════════════════════════════════════════════════
# Strategy 抛错 → 500
# ════════════════════════════════════════════════════════════════════════════
class TestConsolidateFailure:
    @pytest.fixture
    def client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            app_state.consolidation_service = ConsolidationService(strategy=_StubStrategy(fail=True))
            yield c
        reset_prompt_manager_for_test()

    def test_returns_500(self, client):
        body = {"tenant_id": "t1", "user_id": "u1"}
        r = client.post("/v1/memory/consolidate", json=body)
        assert r.status_code == 500


# ════════════════════════════════════════════════════════════════════════════
# OpenAPI 检查
# ════════════════════════════════════════════════════════════════════════════
class TestOpenAPI:
    @pytest.fixture
    def client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            yield c
        reset_prompt_manager_for_test()

    def test_all_mvp_endpoints_registered(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = list(r.json()["paths"].keys())
        for required in (
            "/v1/memory/ingest",
            "/v1/memory/retrieve",
            "/v1/memory/feedback",
            "/v1/memory/consolidate",
        ):
            assert required in paths, f"missing endpoint {required}"
