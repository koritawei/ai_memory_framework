"""Admin 配置 CRUD 路由。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from memory_app.config_center import ConfigValidationError
from memory_app.routers.admin.common import require_config_center

logger = logging.getLogger(__name__)

router = APIRouter()


def safe_history_version(entry: dict) -> int:
    """history 行的 version 字段可能是 str / None；转换失败返回 -1。"""
    try:
        return int(entry.get("version", -1))
    except (TypeError, ValueError):
        return -1


class ConfigWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field(default="global", pattern="^(global|tenant|user)$")
    scope_id: str | None = None
    actor: str = Field(default="ops")
    gray_rules: list[dict[str, Any]] | None = None


class ConfigRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(..., min_length=1)
    target_version: int = Field(..., ge=1)
    scope: str = Field(default="global", pattern="^(global|tenant|user)$")
    scope_id: str | None = None
    actor: str = Field(default="ops")


@router.get("/config")
async def get_config(
    category: str = Query(..., min_length=1),
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
):
    cc = require_config_center()
    try:
        resolved = await cc.resolve(category, tenant_id=tenant_id, user_id=user_id)
    except LookupError as e:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"category not found: {e}"
        )
    return {
        "category": category,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "name": resolved.name,
        "params": resolved.params,
        "version": resolved.version,
        "source": resolved.source,
    }


@router.post("/config")
async def write_config(body: ConfigWriteRequest = Body(...)):
    cc = require_config_center()
    try:
        version = await cc.write(
            body.category,
            body.name,
            body.params,
            scope=body.scope,
            scope_id=body.scope_id,
            actor=body.actor,
            gray_rules=body.gray_rules,
        )
    except ConfigValidationError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"json_pointer": e.json_pointer, "message": e.message},
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info(
        "config write by %s: %s name=%s scope=%s version=%d",
        body.actor, body.category, body.name, body.scope, version,
    )
    return {
        "category": body.category,
        "name": body.name,
        "scope": body.scope,
        "scope_id": body.scope_id,
        "version": version,
        "actor": body.actor,
    }


@router.get("/config/history")
async def get_config_history(
    category: str = Query(..., min_length=1),
    limit: int = Query(default=50, ge=1, le=500),
):
    cc = require_config_center()
    try:
        history = await cc.history(category, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"history failed: {e.__class__.__name__}",
        )
    return {"category": category, "limit": limit, "history": history, "count": len(history)}


@router.post("/config/rollback")
async def rollback_config(body: ConfigRollbackRequest = Body(...)):
    cc = require_config_center()
    try:
        history = await cc.history(body.category, limit=500)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"history failed: {e.__class__.__name__}",
        )
    target = next(
        (
            h for h in history
            if safe_history_version(h) == body.target_version
            and h.get("scope") == body.scope
            and h.get("scope_id") == body.scope_id
        ),
        None,
    )
    if target is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=(
                f"history not found: category={body.category} version={body.target_version}"
                f" scope={body.scope} scope_id={body.scope_id}"
            ),
        )
    try:
        new_version = await cc.write(
            body.category,
            target["name"],
            dict(target.get("params") or {}),
            scope=body.scope,
            scope_id=body.scope_id,
            actor=f"{body.actor}/rollback@v{body.target_version}",
            gray_rules=target.get("variants"),
        )
    except ConfigValidationError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"json_pointer": e.json_pointer, "message": e.message},
        )
    return {
        "category": body.category,
        "rolled_back_to": body.target_version,
        "new_version": new_version,
        "scope": body.scope,
        "scope_id": body.scope_id,
        "actor": body.actor,
    }


__all__ = ["router", "safe_history_version", "ConfigWriteRequest", "ConfigRollbackRequest"]
