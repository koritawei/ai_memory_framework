"""MemorySyncReconciler —— 从 Mongo SOT 重试 DLQ 中的 ES/Milvus 同步。"""

from __future__ import annotations

import logging
from typing import Any

from memory_app.middleware.metrics import (
    DLQ_RECONCILE_FAILED,
    DLQ_RECONCILE_SUCCEEDED,
    DLQ_SIZE,
)
from memory_app.repositories.dlq import DLQRecord

logger = logging.getLogger(__name__)


class SyncReconciler:
    """扫描 DLQ → 读 Mongo MemCell → 重试 ES/Milvus 写入。"""

    def __init__(
        self,
        *,
        dlq: Any,
        mongo_repo: Any,
        es_repo: Any | None,
        milvus_repo: Any | None,
        max_retries: int = 5,
    ) -> None:
        self._dlq = dlq
        self._mongo_repo = mongo_repo
        self._es_repo = es_repo
        self._milvus_repo = milvus_repo
        self._max_retries = max(1, int(max_retries))

    async def stats(self) -> dict[str, Any]:
        total = await self._dlq.size()
        DLQ_SIZE.set(total)
        by_target: dict[str, int] = {}
        for target in ("es", "milvus", "background_task", "redis_task", "cold_path"):
            items = await self._dlq.list(target=target, limit=10_000)
            if items:
                by_target[target] = len(items)
        return {"total": total, "by_target": by_target}

    async def list_records(
        self, *, target: str | None = None, limit: int = 50
    ) -> list[DLQRecord]:
        return await self._dlq.list(target=target, limit=limit)

    async def reconcile(
        self,
        *,
        target: str | None = None,
        limit: int = 100,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        records = await self._dlq.list(target=target, limit=limit)
        result = {
            "dry_run": dry_run,
            "scanned": len(records),
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "exhausted": 0,
            "details": [],
        }
        for rec in records:
            detail = await self._reconcile_one(rec, dry_run=dry_run)
            result["details"].append(detail)
            status = detail["status"]
            if status == "ok":
                result["succeeded"] += 1
            elif status in ("skipped", "dry_run"):
                result["skipped"] += 1
            elif status == "exhausted":
                result["exhausted"] += 1
            else:
                result["failed"] += 1
        await self.stats()
        return result

    async def _reconcile_one(self, rec: DLQRecord, *, dry_run: bool) -> dict:
        base = {
            "target": rec.target,
            "mem_cell_id": rec.mem_cell_id,
            "retry_count": rec.retry_count,
        }
        if rec.retry_count >= self._max_retries:
            return {**base, "status": "exhausted", "error": rec.error}

        if rec.target not in ("es", "milvus"):
            return {
                **base,
                "status": "skipped",
                "error": f"unsupported target: {rec.target}",
            }

        cell = await self._mongo_repo.get_by_id(rec.mem_cell_id)
        if cell is None:
            return {**base, "status": "skipped", "error": "mem_cell not found in mongo"}

        if dry_run:
            return {**base, "status": "dry_run", "error": ""}

        try:
            if rec.target == "es":
                if self._es_repo is None:
                    return {**base, "status": "skipped", "error": "es_repo unavailable"}
                await self._es_repo.index(cell)
            elif rec.target == "milvus":
                if self._milvus_repo is None:
                    return {
                        **base,
                        "status": "skipped",
                        "error": "milvus_repo unavailable",
                    }
                if not cell.embedding:
                    return {
                        **base,
                        "status": "skipped",
                        "error": "embedding not ready (cold path pending)",
                    }
                upsert = getattr(self._milvus_repo, "upsert", None)
                if callable(upsert):
                    await upsert(
                        cell.mem_cell_id,
                        list(cell.embedding),
                        metadata={"tenant_id": cell.tenant_id, "user_id": cell.user_id},
                    )
                else:
                    await self._milvus_repo.insert(
                        cell.mem_cell_id,
                        list(cell.embedding),
                        metadata={"tenant_id": cell.tenant_id, "user_id": cell.user_id},
                    )
        except Exception as e:  # noqa: BLE001
            err = str(e)
            logger.warning(
                "dlq reconcile failed %s/%s: %s",
                rec.target,
                rec.mem_cell_id,
                err,
            )
            bump = getattr(self._dlq, "bump_retry", None)
            if callable(bump):
                await bump(rec.target, rec.mem_cell_id, error=err)
            DLQ_RECONCILE_FAILED.labels(target=rec.target).inc()
            return {**base, "status": "failed", "error": err}

        remove = getattr(self._dlq, "remove", None)
        if callable(remove):
            await remove(rec.target, rec.mem_cell_id)
        DLQ_RECONCILE_SUCCEEDED.labels(target=rec.target).inc()
        return {**base, "status": "ok", "error": ""}


def build_reconciler_from_state(state: Any) -> SyncReconciler | None:
    """从 AppState 构造 reconciler；缺依赖时返回 None。"""
    if state.dlq is None or state.mongo_repo is None or state.settings is None:
        return None
    es_repo = None
    milvus_repo = None
    ingest = state.ingest_service
    if ingest is not None:
        pipeline = getattr(ingest, "_pipeline", None)
        sync_stage = getattr(pipeline, "_sync_stage", None) if pipeline else None
        if sync_stage is not None:
            es_repo = getattr(sync_stage, "_es_repo", None)
            milvus_repo = getattr(sync_stage, "_milvus_repo", None)
    if es_repo is None and state.clients.es_client is not None:
        from memory_app.repositories.es_repo import ESMemCellRepo

        es_repo = ESMemCellRepo(
            state.clients.es_client, index_prefix=state.settings.es_index_prefix
        )
    if milvus_repo is None and state.clients.milvus_connected:
        from memory_app.repositories.milvus_repo import MilvusMemCellRepo

        milvus_repo = MilvusMemCellRepo(state.settings.milvus_collection)
    return SyncReconciler(
        dlq=state.dlq,
        mongo_repo=state.mongo_repo,
        es_repo=es_repo,
        milvus_repo=milvus_repo,
        max_retries=state.settings.dlq_reconcile_max_retries,
    )


__all__ = ["SyncReconciler", "build_reconciler_from_state"]
