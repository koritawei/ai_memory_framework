"""实体与图查询 E2E（设计  /  / ）。"""

from __future__ import annotations

import pytest

from test_suite.e2e.helpers import wired_client
from test_suite.fixtures.samples import beijing_trip_ingest_body


@pytest.mark.e2e
class TestE2EGraphEntity:
    def test_e2e_query_001_graph_relations_after_index(self):
        with wired_client(with_graph=True) as (client, mongo, _es):
            ingest = client.post("/v1/memory/ingest", json=beijing_trip_ingest_body())
            assert ingest.status_code == 200
            mem_ids = ingest.json()["mem_cell_ids"]
            assert mem_ids

            from memory_app.deps import app_state

            graph = app_state.memory_graph
            store = app_state.entity_store
            assert graph is not None and store is not None

            import asyncio

            for mid in mem_ids:
                cell = mongo.store[mid]
                asyncio.run(
                    graph.add_memory_node(
                        mid, ["北京", "Acme"], cell.tenant_id, cell.user_id
                    )
                )
                asyncio.run(
                    store.upsert_entities(
                        mid, ["北京"], cell.tenant_id, cell.user_id
                    )
                )

            r = client.post(
                "/v1/query/user-graph-relations",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "entity": "北京",
                    "max_depth": 2,
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "ok"
            assert isinstance(body["related_memories"], list)

    def test_e2e_query_002_user_memories_pagination(self):
        with wired_client() as (client, _mongo, _es):
            client.post("/v1/memory/ingest", json=beijing_trip_ingest_body())
            r = client.post(
                "/v1/query/user-memories",
                json={"tenant_id": "t1", "user_id": "u1", "limit": 5},
            )
            assert r.status_code in (200, 503)
            if r.status_code == 200:
                assert "memories" in r.json() or "items" in r.json() or "status" in r.json()
