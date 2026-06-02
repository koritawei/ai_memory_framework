"""Mongo 租户 scope 查询参数辅助。"""

from __future__ import annotations


def tenant_scope_kwargs(
    tenant_id: str | None,
    user_id: str | None,
) -> dict[str, str]:
    """当 tenant 与 user 均已知时返回 ``{"tenant_id": ..., "user_id": ...}``。"""
    if tenant_id is None or user_id is None:
        return {}
    return {"tenant_id": tenant_id, "user_id": user_id}


__all__ = ["tenant_scope_kwargs"]
