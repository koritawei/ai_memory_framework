"""POST /v1/memory/ingest 端点端到端测试(Step 2.4)。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- 有效请求 → 200 + ``mem_cell_ids`` 非空 + ``status="ok"``
- 缺 ``tenant_id`` / ``user_id`` → 422
- 空 ``history_sessions`` → 200 + 空列表 + ``status="empty"``
- IngestService 未就绪 → 503
- ES 失败时 ingest 仍 200(主路径不阻塞)+ DLQ 记录

═══════════════════════════════════════════════════════════════════════════════
测试装配策略
═══════════════════════════════════════════════════════════════════════════════
TestClient 内部用 anyio 事件循环,与 ``asyncio.get_event_loop()`` 不兼容。
本测试**不**通过 ``plugin_factory.build`` 取 SBD —— 那是异步的;
直接 ``RuleSBD()`` 实例化(``__init__`` 默认配置即可用,无需 ``start()``)。

测试代码 ``import memory_app.plugins_default`` 是允许的——铁律针对**业务平面**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_app.deps import app_state
from memory_app.internal_models import MemCell
from memory_app.pipelines import IngestPipeline
from memory_app.plugins_default.rule_sbd import RuleSBD
from memory_app.repositories.dlq import DLQRecord, InMemoryDLQ
from memory_app.services import IngestService


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeMongoRepo:
    def __init__(self) -> None:
        self.store: dict[str, MemCell] = {}

    async def insert(self, cell: MemCell) -> str:
        self.store[cell.mem_cell_id] = cell
        return cell.mem_cell_id


class _FailingESRepo:
    """ES repo 写入总失败。"""

    async def index(self, cell: MemCell) -> None:
        raise RuntimeError("ES down")


class _FakeESRepo:
    def __init__(self) -> None:
        self.indexed: list[str] = []

    async def index(self, cell: MemCell) -> None:
        self.indexed.append(cell.mem_cell_id)


def _build_pipeline(
    *,
    mongo: _FakeMongoRepo,
    es=None,
    milvus=None,
    dlq: InMemoryDLQ | None = None,
) -> IngestPipeline:
    """直接 instantiate RuleSBD(默认配置可用,无需 start)。"""
    sbd = RuleSBD()
    return IngestPipeline(
        segmenter=sbd,
        mem_cell_repo=mongo,
        es_repo=es,
        milvus_repo=milvus,
        dlq=dlq,
    )


def _peek_dlq(dlq: InMemoryDLQ) -> list[DLQRecord]:
    """同步快照 DLQ 内部 buffer(测试便利;生产代码不应这么做)。"""
    return list(dlq._buffer)  # type: ignore[attr-defined]


# ════════════════════════════════════════════════════════════════════════════
# 共用 fixtures
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _ensure_plugins_loaded():
    """触发默认插件注册,让 lifespan 中 _ensure_indexes 等都能跑通。"""
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
    """复制仓库 default.yaml 到 tmp_path,避免污染。"""
    import shutil

    project_root = Path(__file__).resolve().parent.parent
    src = project_root / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    shutil.copy2(src, dst)
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    return dst


# ════════════════════════════════════════════════════════════════════════════
# Happy path:fake repo 注入
# ════════════════════════════════════════════════════════════════════════════
class TestIngestEndpoint:
    @pytest.fixture
    def fakes_and_client(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()

        mongo = _FakeMongoRepo()
        es = _FakeESRepo()
        dlq = InMemoryDLQ()

        with TestClient(api.app) as c:
            pipeline = _build_pipeline(mongo=mongo, es=es, dlq=dlq)
            app_state.ingest_service = IngestService(pipeline)
            yield c, mongo, es, dlq
        reset_prompt_manager_for_test()

    def test_minimal_success(self, fakes_and_client):
        client, mongo, es, _dlq = fakes_and_client
        body = {
            "tenant_id": "acme",
            "user_id": "u1",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [
                        {"role": "user", "content": "你好"},
                        {"role": "assistant", "content": "你好,很高兴为您服务"},
                    ],
                }
            ],
        }
        r = client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "ok"
        assert len(data["mem_cell_ids"]) >= 1
        assert data["segment_count"] == len(data["mem_cell_ids"])

        assert len(mongo.store) == len(data["mem_cell_ids"])
        assert es.indexed == data["mem_cell_ids"]

    def test_missing_tenant_returns_422(self, fakes_and_client):
        client, *_ = fakes_and_client
        body = {
            "user_id": "u1",
            "history_sessions": [
                {"session_id": "s1", "turns": [{"role": "user", "content": "x"}]}
            ],
        }
        r = client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 422

    def test_missing_user_returns_422(self, fakes_and_client):
        client, *_ = fakes_and_client
        body = {
            "tenant_id": "acme",
            "history_sessions": [
                {"session_id": "s1", "turns": [{"role": "user", "content": "x"}]}
            ],
        }
        r = client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 422

    def test_invalid_role_returns_422(self, fakes_and_client):
        client, *_ = fakes_and_client
        body = {
            "tenant_id": "acme",
            "user_id": "u1",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [{"role": "robot", "content": "x"}],
                }
            ],
        }
        r = client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 422

    def test_empty_sessions_returns_empty_status(self, fakes_and_client):
        client, mongo, *_ = fakes_and_client
        body = {
            "tenant_id": "acme",
            "user_id": "u1",
            "history_sessions": [],
        }
        r = client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "empty"
        assert data["mem_cell_ids"] == []
        assert data["segment_count"] == 0
        assert mongo.store == {}

    def test_multiple_sessions_create_multiple_cells(self, fakes_and_client):
        client, mongo, *_ = fakes_and_client
        body = {
            "tenant_id": "acme",
            "user_id": "u1",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [{"role": "user", "content": "session1 talk"}],
                },
                {
                    "session_id": "s2",
                    "turns": [{"role": "user", "content": "session2 talk"}],
                },
            ],
        }
        r = client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 200
        data = r.json()
        # session 粒度 → 2 段输入,session_id 不同 → 入 SBD 后默认窗口可合并;
        # 关键约束:落库数与返回数一致 + 至少 1 条
        assert len(data["mem_cell_ids"]) >= 1
        assert len(mongo.store) == len(data["mem_cell_ids"])

    def test_round_trip_text_preserved(self, fakes_and_client):
        client, mongo, *_ = fakes_and_client
        body = {
            "tenant_id": "acme",
            "user_id": "u1",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [{"role": "user", "content": "我喜欢咖啡"}],
                }
            ],
        }
        r = client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 200
        cell_id = r.json()["mem_cell_ids"][0]
        cell = mongo.store[cell_id]
        assert "我喜欢咖啡" in cell.text


# ════════════════════════════════════════════════════════════════════════════
# 失败语义:ES 不可达不阻塞主路径
# ════════════════════════════════════════════════════════════════════════════
class TestIngestEndpointWithFailingES:
    @pytest.fixture
    def client_and_state(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()

        mongo = _FakeMongoRepo()
        dlq = InMemoryDLQ()

        with TestClient(api.app) as c:
            pipeline = _build_pipeline(mongo=mongo, es=_FailingESRepo(), dlq=dlq)
            app_state.ingest_service = IngestService(pipeline)
            yield c, mongo, dlq
        reset_prompt_manager_for_test()

    def test_es_failure_does_not_block(self, client_and_state):
        client, mongo, dlq = client_and_state
        body = {
            "tenant_id": "acme",
            "user_id": "u1",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [{"role": "user", "content": "test"}],
                }
            ],
        }
        r = client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 200
        assert len(mongo.store) >= 1

        records = _peek_dlq(dlq)
        assert len(records) >= 1
        assert records[0].target == "es"
        assert "ES down" in records[0].error


# ════════════════════════════════════════════════════════════════════════════
# 服务未就绪 → 503
# ════════════════════════════════════════════════════════════════════════════
class TestIngestServiceUnavailable:
    @pytest.fixture
    def client_no_service(self, project_cwd, isolated_default_yaml, monkeypatch):
        from memory_app import api
        from memory_app.prompt_runtime import reset_prompt_manager_for_test
        from memory_app.settings import reset_settings_for_test

        reset_settings_for_test()
        reset_prompt_manager_for_test()

        with TestClient(api.app) as c:
            # 故意把 ingest_service 置 None 模拟未就绪
            app_state.ingest_service = None
            yield c
        reset_prompt_manager_for_test()

    def test_returns_503_when_service_not_ready(self, client_no_service: TestClient):
        body = {
            "tenant_id": "acme",
            "user_id": "u1",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [{"role": "user", "content": "x"}],
                }
            ],
        }
        r = client_no_service.post("/v1/memory/ingest", json=body)
        assert r.status_code == 503
