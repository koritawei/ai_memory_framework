"""``/v1/admin/*`` 管理面 API —— 子路由聚合。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from memory_app.routers.admin import config, dlq, plugins, prompts
from memory_app.security.auth import require_admin_auth

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_auth)],
)

router.include_router(plugins.router)
router.include_router(prompts.router)
router.include_router(config.router)
router.include_router(dlq.router)

__all__ = ["router"]
