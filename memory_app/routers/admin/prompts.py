"""Admin Prompt 管理路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, status

from memory_app.config_center import ConfigValidationError, PromptNotFoundError
from memory_app.prompt_manager.manager import _format_template
from memory_app.routers.admin.common import require_config_center

router = APIRouter()


@router.get("/prompts")
async def list_prompts(
    tag: str | None = Query(default=None, description="按 tags 过滤"),
    include_builtin: bool = Query(default=True, description="是否合并内置种子"),
):
    cc = require_config_center()
    ids = await cc.list_prompt_ids(include_builtin=include_builtin, tag=tag)  # type: ignore[attr-defined]
    return {"prompts": ids, "total": len(ids)}


@router.get("/prompts/{prompt_id}")
async def get_prompt(
    prompt_id: str,
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
):
    cc = require_config_center()
    try:
        resolved = await cc.resolve_prompt(  # type: ignore[attr-defined]
            prompt_id, tenant_id=tenant_id, user_id=user_id
        )
    except PromptNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"prompt not found: {prompt_id}"
        )
    return resolved.model_dump()


@router.get("/prompts/{prompt_id}/history")
async def get_prompt_history(
    prompt_id: str,
    limit: int = Query(default=50, ge=1, le=500),
):
    cc = require_config_center()
    history = await cc.history_prompt(prompt_id, limit=limit)  # type: ignore[attr-defined]
    return {"prompt_id": prompt_id, "history": history, "count": len(history)}


@router.put("/prompts/{prompt_id}")
async def write_prompt(
    prompt_id: str,
    body: dict[str, Any] = Body(...),
    scope: str = Query(default="global", pattern="^(global|tenant|user)$"),
    scope_id: str | None = Query(default=None),
    actor: str = Query(default="ops"),
):
    cc = require_config_center()
    try:
        version = await cc.write_prompt(  # type: ignore[attr-defined]
            prompt_id, body, scope=scope, scope_id=scope_id, actor=actor
        )
    except ConfigValidationError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"json_pointer": e.json_pointer, "message": e.message},
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {
        "prompt_id": prompt_id,
        "version": version,
        "scope": scope,
        "scope_id": scope_id,
    }


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(
    prompt_id: str,
    scope: str = Query(default="global", pattern="^(global|tenant|user)$"),
    scope_id: str | None = Query(default=None),
    actor: str = Query(default="ops"),
):
    cc = require_config_center()
    placeholder_body = {
        "template": "<<DELETED>>",
        "variables": [],
        "description": f"deleted by {actor}",
        "tags": ["__deleted__"],
    }
    try:
        version = await cc.write_prompt(  # type: ignore[attr-defined]
            prompt_id, placeholder_body, scope=scope, scope_id=scope_id, actor=actor
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {
        "prompt_id": prompt_id,
        "version": version,
        "scope": scope,
        "scope_id": scope_id,
        "deleted": True,
        "note": "marked as deleted via placeholder; full delete in Phase 8.3",
    }


@router.post("/prompts/{prompt_id}/render")
async def render_prompt(
    prompt_id: str,
    payload: dict[str, Any] = Body(...),
    tenant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
):
    cc = require_config_center()
    variables = payload.get("variables")
    if variables is None:
        variables = {}
    elif not isinstance(variables, dict):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="variables must be a mapping"
        )

    try:
        resolved = await cc.resolve_prompt(  # type: ignore[attr-defined]
            prompt_id, tenant_id=tenant_id, user_id=user_id
        )
    except PromptNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"prompt not found: {prompt_id}"
        )

    try:
        rendered = _format_template(resolved.template, resolved.variables, variables)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))

    return {
        "prompt_id": prompt_id,
        "source": resolved.source,
        "config_version": resolved.config_version,
        "rendered": rendered,
        "template": resolved.template,
        "variables": resolved.variables,
    }


__all__ = ["router"]
