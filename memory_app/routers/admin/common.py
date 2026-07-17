"""Admin 路由共享依赖。"""

from __future__ import annotations

from fastapi import HTTPException, status

from memory_app.deps import app_state


def require_config_center():
    """确保 ConfigCenter 已就绪;否则返回 503。"""
    cc = app_state.config_center
    if cc is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="config_center not initialized"
        )
    return cc


def require_reconciler():
    from memory_app.reconciliation.sync_reconciler import build_reconciler_from_state

    rec = build_reconciler_from_state(app_state)
    if rec is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sync reconciler not available (dlq or mongo_repo missing)",
        )
    return rec


__all__ = ["require_config_center", "require_reconciler"]
