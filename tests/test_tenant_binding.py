"""租户绑定与 JWT 身份解析测试。"""

from __future__ import annotations

import jwt
import pytest
from fastapi import HTTPException

from memory_app.security.identity import (
    ResolvedIdentity,
    resolve_identity,
    validate_body_tenant,
)
from memory_app.settings import Settings
from fastapi.security import HTTPAuthorizationCredentials


def _settings(**overrides) -> Settings:
    base = {
        "app_name": "T",
        "debug": False,
        "mongo_uri": "mongodb://l:1",
        "mongo_db": "m",
        "es_hosts": ["http://l:9200"],
        "es_index_prefix": "p",
        "milvus_host": "h",
        "milvus_port": 1,
        "milvus_collection": "c",
        "redis_url": "redis://l:6379/0",
        "config_center_backend": "file",
        "config_center_file_path": "x.yaml",
        "auth_enabled": True,
        "admin_api_key": None,
        "api_key": "global-key",
        "trust_gateway_headers": False,
        "jwt_secret": "secret",
        "jwt_algorithm": "HS256",
        "api_key_bindings": {"bound-key": {"tenant_id": "tenant_a", "user_id": "u1"}},
        "dlq_backend": "memory",
        "task_runner_backend": "asyncio",
        "task_queue_key": "memory:tasks",
        "task_runner_max_concurrent": 64,
        "cold_path_max_parallel": 64,
        "tenant_binding_enabled": False,
        "metrics_enabled": False,
        "rate_limit_enabled": False,
        "rate_limit_rpm": 120,
        "rate_limit_backend": "memory",
        "rate_limit_fail_open": True,
        "dlq_reconcile_interval_s": 0,
        "dlq_reconcile_batch_size": 100,
        "dlq_reconcile_max_retries": 5,
        "task_runner_consumer_enabled": True,
        "discover_entry_point_plugins": False,
        "plugin_entry_point_group": "memory_app.plugins",
        "strict_readiness": False,
    }
    base.update(overrides)
    return Settings(**base)


def test_api_key_binding_resolves_tenant():
    s = _settings()
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bound-key")
    identity = resolve_identity(s, creds)
    assert identity == ResolvedIdentity(tenant_id="tenant_a", user_id="u1", source="binding")


def test_jwt_resolves_tenant():
    s = _settings()
    token = jwt.encode(
        {"tenant_id": "jwt_t", "user_id": "ju"},
        "secret",
        algorithm="HS256",
    )
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    identity = resolve_identity(s, creds)
    assert identity is not None
    assert identity.tenant_id == "jwt_t"
    assert identity.source == "jwt"


def test_validate_body_tenant_rejects_mismatch():
    identity = ResolvedIdentity(tenant_id="tenant_a", user_id="u1", source="binding")
    with pytest.raises(HTTPException) as exc:
        validate_body_tenant(identity, "other_tenant", "u1")
    assert exc.value.status_code == 403


def test_validate_body_tenant_accepts_match():
    identity = ResolvedIdentity(tenant_id="tenant_a", user_id="u1", source="binding")
    validate_body_tenant(identity, "tenant_a", "u1")
