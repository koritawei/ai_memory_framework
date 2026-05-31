"""统一鉴权逻辑（设计文档 §2.5 横切能力）。

═══════════════════════════════════════════════════════════════════════════════
两道独立密钥
═══════════════════════════════════════════════════════════════════════════════
- **业务面** ``Authorization: Bearer <api_key>`` —— ``Settings.api_key``
- **管理面** ``X-Admin-Key`` —— ``Settings.admin_api_key``

管理面与业务面独立:

- **业务面**: ``auth_enabled=false`` 时放行;``auth_enabled=true`` 时要求 Bearer。
- **管理面**: 已配置 ``admin_api_key`` 时**始终**要求 ``X-Admin-Key`` 匹配
  (即使 ``auth_enabled=false``,便于「业务开放、管理面上锁」);未配置 key 且
  ``auth_enabled=true`` 时 fail-closed 返回 403。

密钥比较一律走 :func:`secrets.compare_digest`,防止时序侧信道。
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)


def verify_secret(provided: str | None, expected: str | None) -> bool:
    """常量时间比较两个密钥字符串；任一方为空则返回 False。"""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def _settings_or_none():
    from memory_app.deps.state import app_state

    return app_state.settings


def check_admin_key(x_admin_key: str | None) -> None:
    """校验管理面 ``X-Admin-Key`` 头。

    - 已配置 ``admin_api_key`` 时始终要求匹配(即使 ``auth_enabled=false``)
    - 未配置 key 且 ``auth_enabled=true`` 时拒绝(防止误配暴露管理面)

    :raises HTTPException: 403 当鉴权失败
    """
    settings = _settings_or_none()
    if settings is None:
        return
    expected = settings.admin_api_key
    if expected is not None:
        if not verify_secret(x_admin_key, expected):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid X-Admin-Key")
        return
    if settings.auth_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="admin_api_key not configured"
        )


def check_api_key(credentials: HTTPAuthorizationCredentials | None) -> None:
    """校验业务面 ``Authorization: Bearer`` 令牌。

    接受三类凭证（满足其一即可）：
    - 全局 ``Settings.api_key``
    - ``Settings.api_key_bindings`` 中登记的租户绑定密钥
    - JWT（形如 ``xxx.yyy.zzz`` 且配置了 ``jwt_secret``，具体 claim 由
      :func:`memory_app.security.identity.resolve_identity` 解析）

    :raises HTTPException: 401/403 当鉴权失败
    """
    settings = _settings_or_none()
    if settings is None:
        return
    if not settings.auth_enabled:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    if settings.api_key and verify_secret(token, settings.api_key):
        return
    if token in (settings.api_key_bindings or {}):
        return
    if settings.jwt_secret and token.count(".") == 2:
        return

    if (
        not settings.api_key
        and not settings.api_key_bindings
        and not settings.jwt_secret
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="api_key not configured"
        )
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin_auth(
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
) -> None:
    """FastAPI 依赖：管理面路由挂载。"""
    check_admin_key(x_admin_key)


async def require_api_auth(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, _bearer_scheme
    ] = None,
) -> None:
    """FastAPI 依赖：业务面路由挂载。"""
    check_api_key(credentials)


__all__ = [
    "verify_secret",
    "check_admin_key",
    "check_api_key",
    "require_admin_auth",
    "require_api_auth",
]
