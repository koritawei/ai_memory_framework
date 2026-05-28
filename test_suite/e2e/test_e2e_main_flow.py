"""E2E 主链路（测试方案  / ）。"""

from __future__ import annotations

import pytest

from test_suite.e2e.helpers import wired_client
from test_suite.fixtures.samples import beijing_trip_ingest_body, minimal_ingest_body


@pytest.mark.e2e
class TestE2EMainFlow:
    """E2E-MAIN-001：写入 → 检索 → 反馈 → 再检索。"""

    def test_ingest_retrieve_feedback_roundtrip(self):
        with wired_client() as (client, mongo, _es):
            ingest = client.post("/v1/memory/ingest", json=minimal_ingest_body())
            assert ingest.status_code == 200, ingest.text
            data = ingest.json()
            assert data["status"] == "ok"
            assert len(data["mem_cell_ids"]) >= 1
            mem_id = data["mem_cell_ids"][0]

            retrieve = client.post(
                "/v1/memory/retrieve",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "query": "出差计划",
                    "top_k": 5,
                },
            )
            assert retrieve.status_code == 200, retrieve.text
            hits = retrieve.json()["memories"]
            assert len(hits) >= 1
            assert any("北京" in h.get("content", "") or "出差" in h.get("content", "") for h in hits)

            fb = client.post(
                "/v1/memory/feedback",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "mem_cell_id": mem_id,
                    "feedback_type": "positive",
                    "signal_value": 1.0,
                },
            )
            assert fb.status_code == 200, fb.text

            before = mongo.store[mem_id].strength
            assert before >= 1.0

            retrieve2 = client.post(
                "/v1/memory/retrieve",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "query": "出差",
                    "top_k": 5,
                },
            )
            assert retrieve2.status_code == 200
            assert len(retrieve2.json()["memories"]) >= 1

    def test_multi_session_ingest_persists(self):
        with wired_client() as (client, mongo, es):
            r = client.post("/v1/memory/ingest", json=beijing_trip_ingest_body())
            assert r.status_code == 200
            ids = r.json()["mem_cell_ids"]
            assert len(ids) >= 1
            assert len(mongo.store) == len(ids)
            assert len(es.indexed) == len(ids)
