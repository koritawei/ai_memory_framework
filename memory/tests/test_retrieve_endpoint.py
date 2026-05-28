"""POST /v1/memory/retrieve 端点测试。

═══════════════════════════════════════════════════════════════════════════════
测试装配策略
═══════════════════════════════════════════════════════════════════════════════
与 写入热路径 测试一样,直接构造 ``RetrievalOrchestrator`` 注入 ``app_state``,
不依赖真实 ES / Milvus / EmbeddingProvider。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_app.deps import app_state
from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.retrieval.fusion import RRFFusion
from memory_app.retrieval.orchestrator import RetrievalOrchestrator
from memory_app.retrieval.reranker import MMRReranker


# ════════════════════════════════════════════════════════════════════════════
# Stubs
# ════════════════════════════════════════════════════════════════════════════
class _StubChannel:
    def __init__(self, hits, *, fail=False):
        self.hits = hits
        self.fail = fail

    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]:
        if self.fail:
            raise RuntimeError("channel failed")
        return list(self.hits)


def _hit(mem_id: str, score: float, source: str = "bm25") -> RankedMemory:
    return RankedMemory(
        memory_id=mem_id,
        memory_type=MemoryType.EPISODIC,
        content=f"content {mem_id}",
        score=score,
        source_channel=source,
    )


def _make_orchestrator(channels) -> RetrievalOrchestrator:
    return RetrievalOrchestrator(
        channels=channels,
        fuser=RRFFusion(),
        filters=[],
        reranker=MMRReranker(),
    )


# ════════════════════════════════════════════════════════════════════════════
# 共用 fixture(沿用 写入热路径 项目根 + 隔离 default.yaml)
# ════════════════════════════════════════════════════════════════════════════
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

    project_root = Path(__file__).resolve().parent.parent
    src = project_root / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    shutil.copy2(src, dst)
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    return dst


# ════════════════════════════════════════════════════════════════════════════
# Happy path
# ════════════════════════════════════════════════════════════════════════════
class TestRetrieveEndpoint:
    @pytest.fixture
    def client_with_orch(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()

        bm25 = _StubChannel(
            [_hit("a", 5.0), _hit("b", 3.0)]
        )
        vec = _StubChannel([_hit("a", 0.9, "vector"), _hit("c", 0.5, "vector")])

        with TestClient(api.app) as c:
            app_state.retrieval_orchestrator = _make_orchestrator(
                {"bm25": bm25, "vector": vec}
            )
            yield c
        reset_prompt_manager_for_test()

    def test_retrieve_success(self, client_with_orch):
        body = {"tenant_id": "t1", "user_id": "u1", "query": "北京", "top_k": 5}
        r = client_with_orch.post("/v1/memory/retrieve", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "memories" in data
        assert data["query"] == "北京"
        assert data["total"] == len(data["memories"])
        ids = {m["memory_id"] for m in data["memories"]}
        assert ids == {"a", "b", "c"}
        # rank / source_channel 已注入 metadata
        assert "rank" in data["memories"][0]["metadata"]
        assert "source_channel" in data["memories"][0]["metadata"]

    def test_top_k_truncation(self, client_with_orch):
        body = {"tenant_id": "t1", "user_id": "u1", "query": "x", "top_k": 2}
        r = client_with_orch.post("/v1/memory/retrieve", json=body)
        assert r.status_code == 200
        assert len(r.json()["memories"]) == 2

    def test_debug_payload(self, client_with_orch):
        body = {"tenant_id": "t1", "user_id": "u1", "query": "x", "top_k": 3, "debug": True}
        r = client_with_orch.post("/v1/memory/retrieve", json=body)
        data = r.json()
        assert data["debug"] is not None

    def test_missing_tenant_returns_422(self, client_with_orch):
        r = client_with_orch.post(
            "/v1/memory/retrieve",
            json={"user_id": "u1", "query": "x"},
        )
        assert r.status_code == 422

    def test_missing_user_returns_422(self, client_with_orch):
        r = client_with_orch.post(
            "/v1/memory/retrieve",
            json={"tenant_id": "t1", "query": "x"},
        )
        assert r.status_code == 422

    def test_missing_query_returns_422(self, client_with_orch):
        r = client_with_orch.post(
            "/v1/memory/retrieve",
            json={"tenant_id": "t1", "user_id": "u1"},
        )
        assert r.status_code == 422

    def test_top_k_out_of_range_422(self, client_with_orch):
        body = {"tenant_id": "t1", "user_id": "u1", "query": "x", "top_k": 200}
        r = client_with_orch.post("/v1/memory/retrieve", json=body)
        assert r.status_code == 422


# ════════════════════════════════════════════════════════════════════════════
# 空库
# ════════════════════════════════════════════════════════════════════════════
class TestRetrieveEmpty:
    @pytest.fixture
    def client_empty(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            app_state.retrieval_orchestrator = _make_orchestrator(
                {"bm25": _StubChannel([])}
            )
            yield c
        reset_prompt_manager_for_test()

    def test_empty_returns_200_empty_list(self, client_empty):
        r = client_empty.post(
            "/v1/memory/retrieve",
            json={"tenant_id": "t1", "user_id": "u1", "query": "x", "top_k": 5},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["memories"] == []
        assert data["total"] == 0


# ════════════════════════════════════════════════════════════════════════════
# 服务未就绪
# ════════════════════════════════════════════════════════════════════════════
class TestRetrieveUnavailable:
    @pytest.fixture
    def client_no_orch(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            app_state.retrieval_orchestrator = None
            yield c
        reset_prompt_manager_for_test()

    def test_returns_503(self, client_no_orch):
        r = client_no_orch.post(
            "/v1/memory/retrieve",
            json={"tenant_id": "t1", "user_id": "u1", "query": "x"},
        )
        assert r.status_code == 503


# ════════════════════════════════════════════════════════════════════════════
# 通道全失败 → 500
# ════════════════════════════════════════════════════════════════════════════
class TestRetrieveAllChannelsFail:
    @pytest.fixture
    def client_failing(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()
        with TestClient(api.app) as c:
            app_state.retrieval_orchestrator = _make_orchestrator(
                {"bm25": _StubChannel([], fail=True)}
            )
            yield c
        reset_prompt_manager_for_test()

    def test_returns_500(self, client_failing):
        r = client_failing.post(
            "/v1/memory/retrieve",
            json={"tenant_id": "t1", "user_id": "u1", "query": "x"},
        )
        assert r.status_code == 500
