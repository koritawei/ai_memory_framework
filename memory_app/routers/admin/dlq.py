"""Admin DLQ / Reconciler 路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from memory_app.routers.admin.common import require_reconciler

logger = logging.getLogger(__name__)

router = APIRouter()


class DLQReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str | None = Field(default=None, description="es | milvus | 省略=全部")
    limit: int = Field(default=100, ge=1, le=1000)
    dry_run: bool = False


@router.get("/dlq")
async def list_dlq(
    target: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    rec = require_reconciler()
    records = await rec.list_records(target=target, limit=limit)
    return {
        "total": len(records),
        "records": [r.model_dump(mode="json") for r in records],
    }


@router.get("/dlq/stats")
async def dlq_stats():
    rec = require_reconciler()
    return await rec.stats()


@router.post("/dlq/reconcile")
async def dlq_reconcile(body: DLQReconcileRequest | None = Body(default=None)):
    req = body or DLQReconcileRequest()
    rec = require_reconciler()
    try:
        report = await rec.reconcile(
            target=req.target,
            limit=req.limit,
            dry_run=req.dry_run,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("dlq reconcile failed: %s", e)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"reconcile failed: {e.__class__.__name__}",
        )
    logger.info(
        "dlq reconcile by admin: scanned=%d ok=%d fail=%d dry_run=%s",
        report["scanned"],
        report["succeeded"],
        report["failed"],
        req.dry_run,
    )
    return report


__all__ = ["router", "DLQReconcileRequest"]
