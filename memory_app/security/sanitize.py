"""连接串脱敏与 Milvus 表达式字面量校验。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# mem_cell_id 等 Milvus 表达式字面量仅允许 UUID/字母数字/连字符/下划线
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


__all__ = ["redact_connection_url", "escape_milvus_expr_string"]
