"""MemorySyncReconciler —— 从 Mongo SOT 重试 DLQ 中的 ES/Milvus 同步。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from memory_app.internal_models import MemCell
from memory_app.middleware.metrics import (
    DLQ_RECONCILE_FAILED,
    DLQ_RECONCILE_SUCCEEDED,
    DLQ_SIZE,
)
from memory_app.repositories.dlq import DLQProto, DLQRecord

logger = logging.getLogger(__name__)

_DEFAULT_MAX_PARALLEL = 8
_STATS_SAMPLE_LIMIT = 10_000


def _dedupe_records(records: list[DLQRecord]) -> list[DLQRecord]:
    """同一 (target, mem_cell_id) 只保留 retry_count 最高的一条。

    DLQ 可能因多次写入失败产生重复项；并发 reconcile 若不去重，
    会对同一 cell 并行重试并重复 bump_retry。
    """
    by_key: dict[tuple[str, str], DLQRecord] = {}
    for rec in records:
        key = (rec.target, rec.mem_cell_id)
        prev = by_key.get(key)
        if prev is None or rec.retry_count >= prev.retry_count:
            by_key[key] = rec
    return list(by_key.values())


class _MongoReadProto(Protocol):
    async def get_by_id(self, mem_cell_id: str) -> MemCell | None: ...

    async def get_by_id_scoped(
        self, mem_cell_id: str, *, tenant_id: str, user_id: str
    ) -> MemCell | None: ...


class _ESIndexProto(Protocol):
    async def index(self, cell: MemCell) -> None: ...


class _MilvusInsertProto(Protocol):
    async def insert(
        self,
        mem_cell_id: str,
        embedding: list[float],
        metadata: dict[str, str] | None,
    ) -> None: ...


class SyncReconciler:
    """扫描 DLQ → 读 Mongo MemCell → 重试 ES/Milvus 写入。"""

    def __init__(
        self,
        *,
        dlq: DLQProto,
        mongo_repo: _MongoReadProto,
        es_repo: _ESIndexProto | None,
        milvus_repo: _MilvusInsertProto | None,
        max_retries: int = 5,
        max_parallel: int = _DEFAULT_MAX_PARALLEL,
    ) -> None:
        self._dlq = dlq
        self._mongo_repo = mongo_repo
        self._es_repo = es_repo
        self._milvus_repo = milvus_repo
        self._max_retries = max(1, int(max_retries))
        self._parallel_sem = asyncio.Semaphore(max(1, int(max_parallel)))

    async def stats(self) -> dict[str, Any]:
        total = await self._dlq.size()
        DLQ_SIZE.set(total)
        by_target: dict[str, int] = {}
        # 单次拉取样本聚合，避免对每个 target 各发一次大 limit 查询
        sample = await self._dlq.list(limit=_STATS_SAMPLE_LIMIT)
        for rec in sample:
            by_target[rec.target] = by_target.get(rec.target, 0) + 1
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
        records = _dedupe_records(await self._dlq.list(target=target, limit=limit))
        result = {
            "dry_run": dry_run,
            "scanned": len(records),
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "exhausted": 0,
            "dry_run_count": 0,
            "details": [],
        }

        async def _guarded(rec: DLQRecord) -> dict:
            async with self._parallel_sem:
                return await self._reconcile_one(rec, dry_run=dry_run)

        details = await asyncio.gather(*(_guarded(rec) for rec in records))
        for detail in details:
            result["details"].append(detail)
            status = detail["status"]
            if status == "ok":
                result["succeeded"] += 1
            elif status == "skipped":
                result["skipped"] += 1
            elif status == "exhausted":
                result["exhausted"] += 1
            elif status == "dry_run":
                result["dry_run_count"] += 1
            else:
                result["failed"] += 1
        await self.stats()
        return result

    async def _load_cell(self, rec: DLQRecord) -> MemCell | None:
        extra = rec.extra or {}
        tenant_id = extra.get("tenant_id")
        user_id = extra.get("user_id")
        scoped_get = getattr(self._mongo_repo, "get_by_id_scoped", None)
        if tenant_id and user_id and callable(scoped_get):
            return await scoped_get(
                rec.mem_cell_id,
                tenant_id=str(tenant_id),
                user_id=str(user_id),
            )
        return await self._mongo_repo.get_by_id(rec.mem_cell_id)

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

        cell = await self._load_cell(rec)
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
            await self._dlq.bump_retry(rec.target, rec.mem_cell_id, error=err)
            DLQ_RECONCILE_FAILED.labels(target=rec.target).inc()
            return {**base, "status": "failed", "error": err}

        await self._dlq.remove(rec.target, rec.mem_cell_id)
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
        sync_repos = getattr(ingest, "sync_index_repos", None)
        if callable(sync_repos):
            es_repo, milvus_repo = sync_repos()
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
