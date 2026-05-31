"""RateLimitMiddleware 集成测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def project_cwd():
    cwd = os.getcwd()
    os.chdir(Path(__file__).resolve().parent.parent)
    try:
        yield
    finally:
        os.chdir(cwd)


def _make_client(monkeypatch, tmp_path, **env_overrides):
    import shutil

    from memory_app import api
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.settings import reset_settings_for_test

    src = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    if src.exists():
        shutil.copy2(src, dst)
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    monkeypatch.setenv("MEMORY_AUTH_ENABLED", "true")
    monkeypatch.setenv("MEMORY_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_RATE_LIMIT_RPM", "2")
    monkeypatch.setenv("MEMORY_RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("MEMORY_RATE_LIMIT_FAIL_OPEN", "false")
    monkeypatch.setenv(
        "MEMORY_API_KEY_BINDINGS",
        '{"key-a":{"tenant_id":"ta","user_id":"u1"},"key-b":{"tenant_id":"tb","user_id":"u2"}}',
    )
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    reset_settings_for_test()
    reset_prompt_manager_for_test()
    return TestClient(api.app)


def _ingest(client: TestClient, token: str):
    return client.post(
        "/v1/memory/ingest",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tenant_id": "ta" if token == "key-a" else "tb",
            "user_id": "u1" if token == "key-a" else "u2",
            "history_sessions": [],
        },
    )


def test_rate_limit_isolated_per_authenticated_identity(
    project_cwd, monkeypatch, tmp_path
):
    """同一 IP 下不同 API Key 绑定身份应各自限流，而非共享 IP 桶。"""
    with _make_client(monkeypatch, tmp_path) as client:
        assert _ingest(client, "key-a").status_code in (200, 503)
        assert _ingest(client, "key-a").status_code in (200, 503)
        r_a_third = _ingest(client, "key-a")
        assert r_a_third.status_code == 429

        # key-b 是另一租户身份，不应被 key-a 的 IP 桶误伤
        r_b_first = _ingest(client, "key-b")
        assert r_b_first.status_code in (200, 503)


def test_rate_limit_key_uses_identity(project_cwd, monkeypatch, tmp_path):
    from memory_app.middleware.rate_limit import RateLimitMiddleware
    from memory_app.security.identity import ResolvedIdentity
    from starlette.requests import Request

    mw = RateLimitMiddleware(None)
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    request = Request(scope)
    request.state.identity = ResolvedIdentity(
        tenant_id="t1", user_id="u1", source="binding"
    )
    assert mw._rate_key(request) == "identity:t1:u1"
