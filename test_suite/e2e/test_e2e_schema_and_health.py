"""Schema / 健康 / Admin 契约（测试方案  /  / ）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from test_suite.fixtures.samples import locomoto_ingest_body, minimal_ingest_body


@pytest.mark.e2e
class TestE2ESchemaContract:
    def test_ut_schema_001_minimal_ingest(self):
        from memory_app.schemas.ingest import MemoryIngestRequest, RawDataType

        req = MemoryIngestRequest(**minimal_ingest_body())
        assert req.raw_data_type == RawDataType.CONVERSATION
        assert req.event_time is not None

    def test_ut_schema_002_missing_tenant(self):
        from memory_app.schemas.ingest import MemoryIngestRequest

        body = minimal_ingest_body()
        del body["tenant_id"]
        with pytest.raises(ValidationError):
            MemoryIngestRequest(**body)

    def test_ut_schema_005_retrieve_top_k_bounds(self):
        from memory_app.schemas.retrieve import RetrieveMemRequest

        with pytest.raises(ValidationError):
            RetrieveMemRequest(tenant_id="t", user_id="u", query="q", top_k=0)
        with pytest.raises(ValidationError):
            RetrieveMemRequest(tenant_id="t", user_id="u", query="q", top_k=101)

    def test_it_ingest_002_missing_tenant_via_http(self, api_client):
        body = minimal_ingest_body()
        del body["tenant_id"]
        r = api_client.post("/v1/memory/ingest", json=body)
        assert r.status_code == 422

    def test_ut_schema_004_locomo_fields(self):
        """LoCoMo 兼容字段可被 Pydantic 解析（UT-SCHEMA-004）。"""
        from memory_app.schemas.ingest import MemoryIngestRequest

        req = MemoryIngestRequest(**locomoto_ingest_body())
        session = req.history_sessions[0]
        assert session.speaker_a == "Alice"
        assert session.turns[0].dia_id == "D1:1"


@pytest.mark.e2e
class TestE2EHealthAndAdmin:
    def test_it_health_001_live(self, api_client):
        r = api_client.get("/health/live")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_it_health_002_ready_checks(self, api_client):
        r = api_client.get("/health/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] in ("ok", "degraded", "fail")
        for key in ("mongo", "es", "redis", "milvus", "config_center", "plugin_registry"):
            assert key in body["checks"]

    def test_it_admin_001_plugins_list(self, api_client):
        r = api_client.get("/v1/admin/plugins")
        assert r.status_code == 200
        cats = r.json()["categories"]
        assert "memory.generation.boundary_detector" in cats
        assert "memory.retrieval.fuser" in cats
