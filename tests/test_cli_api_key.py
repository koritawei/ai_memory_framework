"""CLI --api-key 头注入测试。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from memory_app.cli.transport.http import HttpTransport


@pytest.mark.asyncio
async def test_http_transport_injects_bearer():
    transport = HttpTransport(
        "http://127.0.0.1:8000",
        admin_key=None,
        api_key="biz-secret",
        timeout=5.0,
    )
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["auth"] = req.headers.get("Authorization")
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b"{}"
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", fake_urlopen):
        status, _ = await transport.request(
            "POST",
            "/v1/memory/ingest",
            json_body={"tenant_id": "t1", "user_id": "u1", "history_sessions": []},
        )
    assert status == 200
    assert captured["auth"] == "Bearer biz-secret"
