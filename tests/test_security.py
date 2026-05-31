"""security 模块单元测试。"""

from __future__ import annotations

import pytest

from memory_app.security import (
    escape_milvus_expr_string,
    redact_connection_url,
    verify_admin_key,
    verify_api_key,
)


class TestRedactConnectionUrl:
    def test_redacts_credentials(self):
        url = "mongodb://user:secret@mongo.example.com:27017/memory"
        assert "secret" not in redact_connection_url(url)
        assert "user" not in redact_connection_url(url)
        assert "mongo.example.com" in redact_connection_url(url)

    def test_passthrough_without_credentials(self):
        url = "redis://localhost:6379/0"
        assert redact_connection_url(url) == url


class TestEscapeMilvusExprString:
    def test_accepts_uuid_like_id(self):
        assert escape_milvus_expr_string("abc-123_def") == "abc-123_def"

    def test_rejects_injection_chars(self):
        with pytest.raises(ValueError):
            escape_milvus_expr_string('foo" OR 1==1')


class TestVerifyAdminKey:
    def test_requires_key_when_configured_even_if_auth_disabled(self):
        from types import SimpleNamespace

        settings = SimpleNamespace(auth_enabled=False, admin_api_key="secret")
        with pytest.raises(Exception) as exc:
            verify_admin_key(None, settings)  # type: ignore[arg-type]
        assert exc.value.status_code == 403  # type: ignore[attr-defined]


class TestVerifyApiKey:
    def test_skips_when_auth_disabled(self):
        from types import SimpleNamespace

        settings = SimpleNamespace(auth_enabled=False, admin_api_key=None)
        verify_api_key(None, None, settings)  # type: ignore[arg-type]
