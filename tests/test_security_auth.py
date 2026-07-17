"""security.auth 模块测试。"""

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


def _make_client(
    monkeypatch,
    tmp_path,
    *,
    auth_enabled: bool = False,
    api_key: str | None = None,
    admin_api_key: str | None = None,
):
    import shutil

    from memory_app import api
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.settings import reset_settings_for_test

    src = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    if src.exists():
        shutil.copy2(src, dst)
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    if auth_enabled:
        monkeypatch.setenv("MEMORY_AUTH_ENABLED", "true")
        if api_key:
            monkeypatch.setenv("MEMORY_API_KEY", api_key)
    if admin_api_key:
        monkeypatch.setenv("MEMORY_ADMIN_API_KEY", admin_api_key)
    reset_settings_for_test()
    reset_prompt_manager_for_test()
    return TestClient(api.app)


class TestBusinessApiAuth:
    def test_open_when_auth_disabled(self, project_cwd, monkeypatch, tmp_path):
        with _make_client(monkeypatch, tmp_path) as client:
            r = client.post(
                "/v1/memory/ingest",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "history_sessions": [],
                },
            )
            assert r.status_code in (200, 503)

    def test_401_without_bearer_when_auth_enabled(self, project_cwd, monkeypatch, tmp_path):
        with _make_client(
            monkeypatch, tmp_path, auth_enabled=True, api_key="biz-secret"
        ) as client:
            r = client.post(
                "/v1/memory/ingest",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "history_sessions": [],
                },
            )
            assert r.status_code == 401

    def test_200_with_valid_bearer(self, project_cwd, monkeypatch, tmp_path):
        with _make_client(
            monkeypatch, tmp_path, auth_enabled=True, api_key="biz-secret"
        ) as client:
            r = client.post(
                "/v1/memory/ingest",
                headers={"Authorization": "Bearer biz-secret"},
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "history_sessions": [],
                },
            )
            assert r.status_code in (200, 503)

    def test_health_unaffected_by_auth(self, project_cwd, monkeypatch, tmp_path):
        with _make_client(
            monkeypatch, tmp_path, auth_enabled=True, api_key="biz-secret"
        ) as client:
            r = client.get("/health/live")
            assert r.status_code == 200


class TestRequestIdMiddleware:
    def test_response_includes_request_id(self, project_cwd, monkeypatch, tmp_path):
        with _make_client(monkeypatch, tmp_path) as client:
            r = client.get("/health/live")
            assert r.status_code == 200
            assert "X-Request-Id" in r.headers

    def test_echoes_client_request_id(self, project_cwd, monkeypatch, tmp_path):
        with _make_client(monkeypatch, tmp_path) as client:
            r = client.get("/health/live", headers={"X-Request-Id": "trace-abc"})
            assert r.headers.get("X-Request-Id") == "trace-abc"


class TestAdminKeyIndependentOfAuthSwitch:
    def test_requires_admin_key_when_configured_even_if_auth_disabled(
        self, project_cwd, monkeypatch, tmp_path
    ):
        with _make_client(
            monkeypatch, tmp_path, auth_enabled=False, admin_api_key="admin-secret"
        ) as client:
            r = client.get("/v1/admin/plugins")
            assert r.status_code == 403

    def test_admin_ok_with_valid_key_when_auth_disabled(
        self, project_cwd, monkeypatch, tmp_path
    ):
        with _make_client(
            monkeypatch, tmp_path, auth_enabled=False, admin_api_key="admin-secret"
        ) as client:
            r = client.get(
                "/v1/admin/plugins", headers={"X-Admin-Key": "admin-secret"}
            )
            assert r.status_code == 200

    def test_business_still_open_when_auth_disabled_with_admin_key(
        self, project_cwd, monkeypatch, tmp_path
    ):
        with _make_client(
            monkeypatch, tmp_path, auth_enabled=False, admin_api_key="admin-secret"
        ) as client:
            r = client.post(
                "/v1/memory/ingest",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "history_sessions": [],
                },
            )
            assert r.status_code in (200, 503)
