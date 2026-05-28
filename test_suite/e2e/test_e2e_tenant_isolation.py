"""多租户隔离 E2E（测试方案 ）。"""

from __future__ import annotations

import pytest

from test_suite.e2e.helpers import wired_client
from test_suite.fixtures.samples import tenant_a_coffee_body, tenant_b_tea_body


@pytest.mark.e2e
class TestE2ETenantIsolation:
    def test_retrieve_does_not_leak_across_tenants(self):
        with wired_client() as (client, mongo, _es):
            r_a = client.post("/v1/memory/ingest", json=tenant_a_coffee_body())
            r_b = client.post("/v1/memory/ingest", json=tenant_b_tea_body())
            assert r_a.status_code == 200 and r_b.status_code == 200

            ret_a = client.post(
                "/v1/memory/retrieve",
                json={
                    "tenant_id": "tenant_a",
                    "user_id": "user_1",
                    "query": "喜欢什么饮品",
                    "top_k": 10,
                },
            )
            ret_b = client.post(
                "/v1/memory/retrieve",
                json={
                    "tenant_id": "tenant_b",
                    "user_id": "user_1",
                    "query": "喜欢什么饮品",
                    "top_k": 10,
                },
            )
            assert ret_a.status_code == 200
            assert ret_b.status_code == 200

            contents_a = " ".join(h["content"] for h in ret_a.json()["memories"])
            contents_b = " ".join(h["content"] for h in ret_b.json()["memories"])

            assert "咖啡" in contents_a or contents_a == ""
            assert "茶" in contents_b or contents_b == ""
            assert "茶" not in contents_a
            assert "咖啡" not in contents_b

            for cell in mongo.store.values():
                assert cell.tenant_id in ("tenant_a", "tenant_b")
