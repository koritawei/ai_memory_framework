"""认知分层数据流 E2E（设计  /  / ）。"""

from __future__ import annotations

import pytest

from test_suite.fixtures.samples import minimal_ingest_body, time_gap_ingest_body


@pytest.mark.e2e
class TestE2ECognitiveLayers:
    def test_e2e_cog_001_format_transfer_chain(self):
        from memory_app.format_transfer import ingest_to_raw_data_list
        from memory_app.schemas.ingest import MemoryIngestRequest

        req = MemoryIngestRequest(**minimal_ingest_body())
        raw_list = ingest_to_raw_data_list(req)
        assert len(raw_list) == 1
        assert raw_list[0].tenant_id == "t1"
        assert "北京" in raw_list[0].content

    def test_e2e_cog_002_memcell_tenant_traceability(self):
        from memory_app.internal_models import MemCell

        cell = MemCell(
            tenant_id="t1",
            user_id="u1",
            session_id="s1",
            text="我下周要去北京出差",
        )
        assert cell.mem_cell_id
        assert cell.tenant_id == "t1"
        assert cell.strength == 1.0

    def test_e2e_cog_003_time_gap_sample_parseable(self):
        from memory_app.schemas.ingest import MemoryIngestRequest

        req = MemoryIngestRequest(**time_gap_ingest_body(gap_minutes=45))
        assert len(req.history_sessions[0].turns) == 2
        assert req.history_sessions[0].turns[0].timestamp is not None
