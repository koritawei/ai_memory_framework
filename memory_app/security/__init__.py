"""横切安全能力：API 鉴权、密钥校验、连接串脱敏。"""

from memory_app.security.sanitize import escape_milvus_expr_string, redact_connection_url

__all__ = [
    "verify_secret",
    "check_admin_key",
    "check_api_key",
    "require_admin_auth",
    "require_api_auth",
    "redact_connection_url",
    "escape_milvus_expr_string",
]

_AUTH_EXPORTS = frozenset(
    {
        "verify_secret",
        "check_admin_key",
        "check_api_key",
        "require_admin_auth",
        "require_api_auth",
    }
)


def __getattr__(name: str):
    if name in _AUTH_EXPORTS:
        from memory_app.security import auth as _auth

        return getattr(_auth, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
