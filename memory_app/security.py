"""安全工具：连接串脱敏与 API 鉴权。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from memory_app.settings import Settings

# mem_cell_id 等 Milvus 表达式字面量仅允许 UUID/字母数字/连字符
_SAFE_MILVUS_ID_RE = re.compile(r"^[\w\-]+$")


def redact_connection_url(url: str) -> str:
    """脱敏连接 URL，隐藏 userinfo 中的凭证。"""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<redacted>"
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"***:***@{host}" if host else "***:***"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def escape_milvus_expr_string(value: str) -> str:
    """校验并转义 Milvus 表达式中的字符串字面量，防止注入。"""
    if not _SAFE_MILVUS_ID_RE.fullmatch(value):
        raise ValueError(f"invalid identifier for milvus expression: {value!r}")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def verify_api_key(
    x_admin_key: str | None,
    x_api_key: str | None,
    settings: "Settings",
) -> None:
    """校验业务面 API Key；``auth_enabled=false`` 时跳过。"""
    if not settings.auth_enabled:
        return
    expected = settings.admin_api_key
    if expected is None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="auth_enabled but admin_api_key not configured",
        )
    provided = x_admin_key or x_api_key
    if provided != expected:
        from fastapi import HTTPException, status

        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="invalid or missing API key (X-API-Key or X-Admin-Key)",
        )


def verify_admin_key(x_admin_key: str | None, settings: "Settings") -> None:
    """校验管理面 X-Admin-Key。

    - 已配置 ``admin_api_key`` 时始终要求匹配（即使 ``auth_enabled=false``）
    - 未配置 key 且 ``auth_enabled=true`` 时拒绝（防止误配暴露管理面）
    """
    from fastapi import HTTPException, status

    expected = settings.admin_api_key
    if expected is not None:
        if x_admin_key != expected:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail="invalid X-Admin-Key"
            )
        return
    if settings.auth_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="admin_api_key not configured"
        )


__all__ = [
    "redact_connection_url",
    "escape_milvus_expr_string",
    "verify_api_key",
    "verify_admin_key",
]
