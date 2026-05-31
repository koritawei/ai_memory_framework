"""security.sanitize 模块单元测试。"""

from __future__ import annotations

import pytest

from memory_app.security.sanitize import escape_milvus_expr_string, redact_connection_url


class TestRedactConnectionUrl:
    def test_redacts_credentials(self):
        url = "mongodb://user:secret@mongo.example.com:27017/memory"
        redacted = redact_connection_url(url)
        assert "secret" not in redacted
        assert "user" not in redacted
        assert "mongo.example.com" in redacted

    def test_passthrough_without_credentials(self):
        url = "redis://localhost:6379/0"
        assert redact_connection_url(url) == url


class TestEscapeMilvusExprString:
    def test_accepts_uuid_like_id(self):
        assert escape_milvus_expr_string("abc-123_def") == "abc-123_def"

    def test_rejects_injection_chars(self):
        with pytest.raises(ValueError, match="invalid identifier"):
            escape_milvus_expr_string('foo" OR 1==1')
