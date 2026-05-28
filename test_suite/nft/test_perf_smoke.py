"""轻量性能烟测（方案，fake profile）。"""

from __future__ import annotations

import time

import pytest

from test_suite.e2e.helpers import wired_client
from test_suite.fixtures.samples import minimal_ingest_body


@pytest.mark.nft
class TestPerfSmoke:
    def test_nft_perf_001_ingest_p95_under_2s_fake(self):
        with wired_client() as (client, _mongo, _es):
            latencies: list[float] = []
            for _ in range(20):
                t0 = time.perf_counter()
                r = client.post("/v1/memory/ingest", json=minimal_ingest_body())
                latencies.append(time.perf_counter() - t0)
                assert r.status_code == 200
            latencies.sort()
            p95 = latencies[int(len(latencies) * 0.95) - 1]
            assert p95 < 2.0, f"ingest p95={p95:.3f}s exceeds 2s (fake profile)"

    def test_nft_perf_002_retrieve_under_1s_fake(self):
        with wired_client() as (client, _mongo, _es):
            client.post("/v1/memory/ingest", json=minimal_ingest_body())
            t0 = time.perf_counter()
            r = client.post(
                "/v1/memory/retrieve",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "query": "北京",
                    "top_k": 10,
                },
            )
            elapsed = time.perf_counter() - t0
            assert r.status_code == 200
            assert elapsed < 1.0, f"retrieve took {elapsed:.3f}s"
