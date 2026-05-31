"""Admin DLQ 端点测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memory_app.repositories.dlq import DLQRecord


def _enqueue_sync(dlq, record: DLQRecord) -> None:
    """测试辅助：绕过 async lock 直接入队。"""
    dlq._buffer.append(record)


@pytest.fixture
def admin_client(project_cwd, tmp_path, monkeypatch):
    import shutil

    from memory_app import api
    from memory_app.deps import app_state
    from memory_app.internal_models import MemCell, MemoryState
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.repositories.dlq import InMemoryDLQ
    from memory_app.settings import get_settings, reset_settings_for_test

    src = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    if src.exists():
        shutil.copy2(src, dst)
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    reset_settings_for_test()
    reset_prompt_manager_for_test()

    cell = MemCell(
        tenant_id="t1", user_id="u1", session_id="s1", text="x", state=MemoryState.ACTIVE
    )
    dlq = InMemoryDLQ()

    class _FakeMongo:
        async def get_by_id(self, mid: str):
            return cell if mid == cell.mem_cell_id else None

    class _FakeES:
        async def index(self, _c):
            return None

    fake_mongo = _FakeMongo()
    fake_ingest = type(
        "S",
        (),
        {
            "_pipeline": type(
                "P",
                (),
                {
                    "_sync_stage": type(
                        "St", (), {"_es_repo": _FakeES(), "_milvus_repo": None}
                    )()
                },
            )()
        },
    )()

    with TestClient(api.app) as client:
        # lifespan init 会覆盖 app_state，需在启动后再注入 fake
        app_state.dlq = dlq
        app_state.mongo_repo = fake_mongo
        app_state.settings = get_settings()
        app_state.ingest_service = fake_ingest
        yield client, dlq, cell
    reset_prompt_manager_for_test()


@pytest.fixture
def project_cwd():
    cwd = os.getcwd()
    os.chdir(Path(__file__).resolve().parent.parent)
    try:
        yield
    finally:
        os.chdir(cwd)


def test_list_dlq(admin_client):
    client, dlq, cell = admin_client
    _enqueue_sync(dlq, DLQRecord(target="es", mem_cell_id=cell.mem_cell_id, error="e"))
    r = client.get("/v1/admin/dlq")
    assert r.status_code == 200
    assert r.json()["total"] >= 1


def test_reconcile_endpoint(admin_client):
    client, dlq, cell = admin_client
    _enqueue_sync(dlq, DLQRecord(target="es", mem_cell_id=cell.mem_cell_id, error="e"))
    r = client.post("/v1/admin/dlq/reconcile", json={"limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["scanned"] >= 1
    assert body["succeeded"] >= 1
