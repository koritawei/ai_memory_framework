"""配置中心与管理面 E2E（设计 §2.8）。"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
class TestE2EConfigAdmin:
    def test_e2e_conf_001_read_fuser_config(self, api_client):
        r = api_client.get(
            "/v1/admin/config",
            params={"category": "memory.retrieval.fuser"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "name" in body or "params" in body or "source" in body

    def test_e2e_conf_002_invalid_config_rejected(self, api_client):
        r = api_client.post(
            "/v1/admin/config",
            json={
                "category": "memory.retrieval.fuser",
                "name": "weighted_rrf",
                "params": {"k": 99999},
            },
        )
        assert r.status_code in (400, 422), r.text
