"""Admin 插件管理路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status

from memory_app.deps import app_state
from memory_app.plugins import registry as plugin_registry

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/plugins")
async def list_plugins():
    """列出所有已注册插件 + 当前活动实例。"""
    return {
        "categories": plugin_registry.describe(),
        "active": app_state.plugin_factory.list_active() if app_state.plugin_factory else [],
    }


@router.get("/plugins/health")
async def plugins_health():
    """聚合所有活动插件实例的 ``health()`` 输出。"""
    if app_state.plugin_factory is None:
        return {}
    return await app_state.plugin_factory.healthcheck_all()


@router.get("/plugins/{category}/{name}/health")
async def plugin_health(category: str, name: str):
    """单个插件实例健康检查。"""
    if app_state.plugin_factory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="plugin_factory not initialized"
        )
    return {
        "category": category,
        "name": name,
        **(await app_state.plugin_factory.health_of(category, name)),
    }


@router.post("/plugins/{category}/{name}/reload")
async def plugin_reload(
    category: str,
    name: str,
    actor: str = Query(default="ops"),
):
    """手工触发指定插件重载(stop+丢弃 + 下次 build 重建)。"""
    if app_state.plugin_factory is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="plugin_factory not initialized"
        )
    released = await app_state.plugin_factory.release_category(category, name)
    logger.info(
        "manual reload by %s: %s/%s released_count=%d", actor, category, name, released
    )
    return {
        "category": category,
        "name": name,
        "released_count": released,
        "actor": actor,
    }


__all__ = ["router"]
