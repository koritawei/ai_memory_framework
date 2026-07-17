"""SyncReconciler 单元测试。"""

from __future__ import annotations

import pytest

from memory_app.internal_models import MemCell, MemoryState
from memory_app.repositories.dlq import DLQRecord, InMemoryDLQ
from memory_app.reconciliation.sync_reconciler import SyncReconciler


class _FakeMongo:
    def __init__(self, cells: dict[str, MemCell]):
        self._cells = cells

    async def get_by_id(self, mid: str):
        return self._cells.get(mid)


class _FakeES:
    def __init__(self):
        self.indexed: list[str] = []

    async def index(self, cell: MemCell) -> None:
        self.indexed.append(cell.mem_cell_id)


@pytest.mark.asyncio
async def test_reconcile_es_success_removes_dlq():
    cell = MemCell(
        tenant_id="t1", user_id="u1", session_id="s1", text="hello", state=MemoryState.ACTIVE
    )
    dlq = InMemoryDLQ()
    await dlq.enqueue(
        DLQRecord(target="es", mem_cell_id=cell.mem_cell_id, error="timeout")
    )
    es = _FakeES()
    rec = SyncReconciler(
        dlq=dlq,
        mongo_repo=_FakeMongo({cell.mem_cell_id: cell}),
        es_repo=es,
        milvus_repo=None,
        max_retries=5,
    )
    report = await rec.reconcile(limit=10)
    assert report["succeeded"] == 1
    assert await dlq.size() == 0
    assert cell.mem_cell_id in es.indexed


@pytest.mark.asyncio
async def test_reconcile_dry_run_does_not_remove():
    cell = MemCell(
        tenant_id="t1", user_id="u1", session_id="s1", text="hello", state=MemoryState.ACTIVE
    )
    dlq = InMemoryDLQ()
    await dlq.enqueue(
        DLQRecord(target="es", mem_cell_id=cell.mem_cell_id, error="timeout")
    )
    es = _FakeES()
    rec = SyncReconciler(
        dlq=dlq,
        mongo_repo=_FakeMongo({cell.mem_cell_id: cell}),
        es_repo=es,
        milvus_repo=None,
    )
    report = await rec.reconcile(limit=10, dry_run=True)
    assert report["succeeded"] == 0
    assert report["failed"] == 0
    assert report["skipped"] == 1
    assert report["details"][0]["status"] == "dry_run"
    assert await dlq.size() == 1
    assert es.indexed == []


@pytest.mark.asyncio
async def test_reconcile_failure_bumps_retry():
    cell = MemCell(
        tenant_id="t1", user_id="u1", session_id="s1", text="hello", state=MemoryState.ACTIVE
    )
    dlq = InMemoryDLQ()
    await dlq.enqueue(
        DLQRecord(target="es", mem_cell_id=cell.mem_cell_id, error="old", retry_count=0)
    )

    class _FailingES:
        async def index(self, _cell):
            raise RuntimeError("es down")

    rec = SyncReconciler(
        dlq=dlq,
        mongo_repo=_FakeMongo({cell.mem_cell_id: cell}),
        es_repo=_FailingES(),
        milvus_repo=None,
    )
    report = await rec.reconcile(limit=10)
    assert report["failed"] == 1
    items = await dlq.list()
    assert items[0].retry_count == 1
    assert "es down" in items[0].error
