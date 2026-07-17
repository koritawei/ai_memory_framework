"""API 身份解析 —— JWT / API Key 到 tenant_id 映射。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from typing import Any

import jwt
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from memory_app.security.auth import verify_secret
from memory_app.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedIdentity:
    """鉴权后解析出的租户 / 用户身份。"""

    tenant_id: str
    user_id: str | None = None
    source: str = "api_key"  # api_key | jwt | binding


def _binding_for_key(settings: Settings, api_key: str) -> ResolvedIdentity | None:
    bindings = settings.api_key_bindings or {}
    raw = bindings.get(api_key)
    if not raw:
        return None
    tenant_id = raw.get("tenant_id")
    if not tenant_id:
        return None
    return ResolvedIdentity(
        tenant_id=str(tenant_id),
        user_id=(str(raw["user_id"]) if raw.get("user_id") else None),
        source="binding",
    )


def _identity_from_jwt(settings: Settings, token: str) -> ResolvedIdentity | None:
    if not settings.jwt_secret:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm]
            if settings.jwt_algorithm in ("HS256", "HS384", "HS512", "RS256")
            else ["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError as e:
        logger.debug("jwt decode failed: %s", e)
        return None
    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        return None
    user_id = payload.get("user_id") or payload.get("sub")
    return ResolvedIdentity(
        tenant_id=str(tenant_id),
        user_id=str(user_id) if user_id else None,
        source="jwt",
    )


def resolve_identity(
    settings: Settings,
    credentials: HTTPAuthorizationCredentials | None,
) -> ResolvedIdentity | None:
    """从 Bearer 令牌解析租户身份；auth 关闭时返回 None。"""
    if not settings.auth_enabled or credentials is None:
        return None
    token = credentials.credentials
    if not token:
        return None
    # JWT 形态: xxx.yyy.zzz
    if token.count(".") == 2:
        jwt_id = _identity_from_jwt(settings, token)
        if jwt_id is not None:
            return jwt_id
    binding = _binding_for_key(settings, token)
    if binding is not None:
        return binding
    # 全局 api_key 未绑定 tenant —— 可配合 trust_gateway_headers
    if settings.api_key and verify_secret(token, settings.api_key):
        return None
    return None


def identity_from_gateway_headers(request_headers: Any) -> ResolvedIdentity | None:
    """网关注入的 ``X-Tenant-Id`` / ``X-User-Id`` 头。"""
    tenant_id = request_headers.get("X-Tenant-Id") or request_headers.get("x-tenant-id")
    if not tenant_id:
        return None
    user_id = request_headers.get("X-User-Id") or request_headers.get("x-user-id")
    return ResolvedIdentity(
        tenant_id=str(tenant_id),
        user_id=str(user_id) if user_id else None,
        source="gateway",
    )


def validate_body_tenant(
    identity: ResolvedIdentity,
    body_tenant_id: str,
    body_user_id: str | None,
) -> None:
    """校验请求体 tenant/user 与鉴权身份一致。

    :raises HTTPException: 403 当不匹配
    """
    if identity.tenant_id != body_tenant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="tenant_id does not match authenticated identity",
        )
    if identity.user_id is not None and body_user_id is not None:
        if identity.user_id != body_user_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="user_id does not match authenticated identity",
            )


__all__ = [
    "ResolvedIdentity",
    "resolve_identity",
    "identity_from_gateway_headers",
    "validate_body_tenant",
]
