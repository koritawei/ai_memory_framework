"""离线巩固 E2E（设计 §7 / 方案 E2E-CONS-001）。"""

from __future__ import annotations

import pytest

from test_suite.e2e.helpers import wired_consolidate_client


@pytest.mark.e2e
class TestE2EConsolidate:
    def test_e2e_cons_001_tenant_consolidate(self):
        with wired_consolidate_client() as (client, strategy):
            r = client.post("/v1/memory/consolidate", json={"tenant_id": "t1"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("scanned_count", 0) >= 0
            assert strategy.calls

    def test_e2e_cons_002_empty_scope_ok(self):
        with wired_consolidate_client() as (client, _strategy):
            r = client.post(
                "/v1/memory/consolidate",
                json={"tenant_id": "t1", "user_id": "u1", "scope": "light"},
            )
            assert r.status_code == 200
