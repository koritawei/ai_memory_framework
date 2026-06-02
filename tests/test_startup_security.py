"""启动期安全配置校验。"""

from __future__ import annotations

import pytest

from memory_app.security.startup import validate_startup_security
from memory_app.settings import Settings


def _settings(**overrides) -> Settings:
    base = {
        "app_name": "test",
        "debug": False,
        "auth_enabled": True,
        "admin_api_key": "secret",
    }
    base.update(overrides)
    return Settings(**base)


def test_debug_skips_checks():
    validate_startup_security(_settings(debug=True, auth_enabled=False))


def test_production_requires_auth():
    with pytest.raises(RuntimeError, match="auth_enabled=false"):
        validate_startup_security(_settings(auth_enabled=False))


def test_production_requires_admin_key():
    with pytest.raises(RuntimeError, match="admin_api_key"):
        validate_startup_security(_settings(admin_api_key=None))
