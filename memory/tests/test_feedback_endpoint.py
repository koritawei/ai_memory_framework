"""POST /v1/memory/feedback 端点测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_app.deps import app_state
from memory_app.internal_models import MemCell, MemoryState
from memory_app.plugins_default.synaptic_reinforcer import SynapticPlasticityReinforcer
from memory_app.services import FeedbackService


class _FakeMongoRepo:
    def __init__(self):
        self.store: dict[str, MemCell] = {}
        self.updates: list[tuple[str, dict]] = []

    async def insert(self, cell):
        self.store[cell.mem_cell_id] = cell
        return cell.mem_cell_id

    async def get_by_id(self, mid):
        return self.store.get(mid)

    async def update(self, mid, updates):
        if mid not in self.store:
            return False
        self.updates.append((mid, dict(updates)))
        cell = self.store[mid]
        for k, v in updates.items():
            setattr(cell, k, v)
        return True


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
class TestFeedbackEndpoint:
    @pytest.fixture
    def fixtures(self, project_cwd, isolated_default_yaml, monkeypatch):
        import asyncio

        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        repo = _FakeMongoRepo()

        reinforcer = SynapticPlasticityReinforcer()
        # SynapticPlasticityReinforcer.start 实际不依赖事件循环;直接 asyncio.run
        asyncio.run(reinforcer.start({}))

        with TestClient(api.app) as c:
            app_state.feedback_service = FeedbackService(
                mongo_repo=repo, reinforcer=reinforcer
            )
            yield c, repo
        reset_prompt_manager_for_test()

    def test_positive_feedback(self, fixtures):
        client, repo = fixtures
        cell = MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="test", strength=1.0,
        )
        # 同步 insert(测试便利)
        repo.store[cell.mem_cell_id] = cell

        body = {
            "tenant_id": "t1", "user_id": "u1",
            "mem_cell_id": cell.mem_cell_id,
            "feedback_type": "positive",
        }
        r = client.post("/v1/memory/feedback", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["mem_cell_id"] == cell.mem_cell_id
        assert data["feedback_type"] == "positive"
        assert data["new_strength"] == pytest.approx(1.09)
        assert data["delta"] == pytest.approx(0.09)
        assert data["access_count"] == 1
        assert data["status"] == "ok"

    def test_negative_feedback(self, fixtures):
        client, repo = fixtures
        cell = MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="x", strength=2.0,
        )
        repo.store[cell.mem_cell_id] = cell

        body = {
            "tenant_id": "t1", "user_id": "u1",
            "mem_cell_id": cell.mem_cell_id,
            "feedback_type": "negative",
        }
        r = client.post("/v1/memory/feedback", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["delta"] < 0
        assert data["access_count"] == 0  # 负向不计

    def test_explicit_signal_value(self, fixtures):
        client, repo = fixtures
        cell = MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="x", strength=1.0,
        )
        repo.store[cell.mem_cell_id] = cell

        body = {
            "tenant_id": "t1", "user_id": "u1",
            "mem_cell_id": cell.mem_cell_id,
            "feedback_type": "positive",
            "signal_value": 2.0,
        }
        r = client.post("/v1/memory/feedback", json=body)
        assert r.status_code == 200
        # 1.0 + 0.3 * 2.0 = 1.6
        assert r.json()["new_strength"] == pytest.approx(1.6)

    def test_not_found_returns_404(self, fixtures):
        client, _ = fixtures
        body = {
            "tenant_id": "t1", "user_id": "u1",
            "mem_cell_id": "nonexistent-id",
            "feedback_type": "positive",
        }
        r = client.post("/v1/memory/feedback", json=body)
        assert r.status_code == 404

    def test_missing_id_returns_422(self, fixtures):
        client, _ = fixtures
        body = {
            "tenant_id": "t1", "user_id": "u1",
            "feedback_type": "positive",
        }
        r = client.post("/v1/memory/feedback", json=body)
        assert r.status_code == 422

    def test_missing_tenant_returns_422(self, fixtures):
        client, _ = fixtures
        body = {
            "user_id": "u1",
            "mem_cell_id": "x",
            "feedback_type": "positive",
        }
        r = client.post("/v1/memory/feedback", json=body)
        assert r.status_code == 422

    def test_invalid_feedback_type_returns_422(self, fixtures):
        client, _ = fixtures
        body = {
            "tenant_id": "t1", "user_id": "u1",
            "mem_cell_id": "x",
            "feedback_type": "implicit_hit",  # 不在枚举里
        }
        r = client.post("/v1/memory/feedback", json=body)
        assert r.status_code == 422


# ════════════════════════════════════════════════════════════════════════════
# 服务未就绪
# ════════════════════════════════════════════════════════════════════════════
class TestFeedbackServiceUnavailable:
    @pytest.fixture
    def client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            app_state.feedback_service = None
            yield c
        reset_prompt_manager_for_test()

    def test_503(self, client):
        body = {
            "tenant_id": "t1", "user_id": "u1",
            "mem_cell_id": "x",
            "feedback_type": "positive",
        }
        r = client.post("/v1/memory/feedback", json=body)
        assert r.status_code == 503
