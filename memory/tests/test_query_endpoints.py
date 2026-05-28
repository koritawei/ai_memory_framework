"""POST /v1/query/* 端点测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_app.deps import app_state
from memory_app.graph_index import InMemoryGraph, MemoryGraph
from memory_app.internal_models import MemCell


class _FakeRepo:
    def __init__(self):
        self.store: dict[str, MemCell] = {}

    async def insert(self, c):
        self.store[c.mem_cell_id] = c

    async def find_all(self, tenant, user, limit=10000):
        return [
            c for c in self.store.values()
            if c.tenant_id == tenant and c.user_id == user
        ][:limit]


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
# /v1/query/user-graph-relations
# ════════════════════════════════════════════════════════════════════════════
class TestUserGraphRelations:
    @pytest.fixture
    def client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()

        # 装配 in-memory MemoryGraph + 灌入数据
        store = InMemoryGraph()
        graph = MemoryGraph(store)

        with TestClient(api.app) as c:
            # 测试期间灌入图数据
            import asyncio
            asyncio.run(graph.add_memory_node("mc1", ["北京"], "t1", "u1"))
            asyncio.run(graph.add_memory_node("mc2", ["北京"], "t1", "u1"))
            asyncio.run(graph.add_memory_node("mc3", ["上海"], "t1", "u1"))
            app_state.memory_graph = graph
            yield c
        reset_prompt_manager_for_test()
        app_state.memory_graph = None

    def test_returns_related_memories(self, client):
        body = {"tenant_id": "t1", "user_id": "u1", "entity": "北京"}
        r = client.post("/v1/query/user-graph-relations", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "ok"
        assert data["entity"] == "北京"
        assert set(data["related_memories"]) == {"mc1", "mc2"}

    def test_unknown_entity_returns_empty_list(self, client):
        body = {"tenant_id": "t1", "user_id": "u1", "entity": "巴黎"}
        r = client.post("/v1/query/user-graph-relations", json=body)
        assert r.status_code == 200
        assert r.json()["related_memories"] == []

    def test_max_depth_param(self, client):
        body = {
            "tenant_id": "t1", "user_id": "u1",
            "entity": "北京", "max_depth": 1,
        }
        r = client.post("/v1/query/user-graph-relations", json=body)
        assert r.status_code == 200

    def test_missing_entity_returns_422(self, client):
        body = {"tenant_id": "t1", "user_id": "u1"}
        r = client.post("/v1/query/user-graph-relations", json=body)
        assert r.status_code == 422

    def test_missing_tenant_returns_422(self, client):
        body = {"user_id": "u1", "entity": "北京"}
        r = client.post("/v1/query/user-graph-relations", json=body)
        assert r.status_code == 422

    def test_max_depth_out_of_range(self, client):
        body = {
            "tenant_id": "t1", "user_id": "u1",
            "entity": "北京", "max_depth": 99,
        }
        r = client.post("/v1/query/user-graph-relations", json=body)
        assert r.status_code == 422


# ════════════════════════════════════════════════════════════════════════════
# 未装配 → not_implemented
# ════════════════════════════════════════════════════════════════════════════
class TestUserGraphRelationsNotImplemented:
    @pytest.fixture
    def client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            app_state.memory_graph = None
            yield c
        reset_prompt_manager_for_test()

    def test_returns_not_implemented(self, client):
        body = {"tenant_id": "t1", "user_id": "u1", "entity": "x"}
        r = client.post("/v1/query/user-graph-relations", json=body)
        assert r.status_code == 200
        assert r.json()["status"] == "not_implemented"


# ════════════════════════════════════════════════════════════════════════════
# /v1/query/user-memories
# ════════════════════════════════════════════════════════════════════════════
class TestUserMemories:
    @pytest.fixture
    def client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        repo = _FakeRepo()

        with TestClient(api.app) as c:
            import asyncio

            for i in range(3):
                asyncio.run(repo.insert(MemCell(
                    tenant_id="t1", user_id="u1", session_id="s1",
                    text=f"x{i}",
                )))
            app_state.mongo_repo = repo
            yield c
        reset_prompt_manager_for_test()
        app_state.mongo_repo = None

    def test_lists_memories(self, client):
        body = {"tenant_id": "t1", "user_id": "u1", "limit": 10}
        r = client.post("/v1/query/user-memories", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "ok"
        assert data["total"] == 3
        assert len(data["memories"]) == 3

    def test_limit(self, client):
        body = {"tenant_id": "t1", "user_id": "u1", "limit": 2}
        r = client.post("/v1/query/user-memories", json=body)
        assert r.status_code == 200
        assert len(r.json()["memories"]) == 2

    def test_limit_out_of_range(self, client):
        body = {"tenant_id": "t1", "user_id": "u1", "limit": 999}
        r = client.post("/v1/query/user-memories", json=body)
        assert r.status_code == 422


# ════════════════════════════════════════════════════════════════════════════
# OpenAPI:图与实体 端点齐全
# ════════════════════════════════════════════════════════════════════════════
class TestOpenAPIIncludesQueryRoutes:
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

    def test_query_routes_registered(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = list(r.json()["paths"].keys())
        assert "/v1/query/user-graph-relations" in paths
        assert "/v1/query/user-memories" in paths
