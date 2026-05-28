"""降级演练 E2E（设计  / 方案 ）。"""

from __future__ import annotations

import asyncio

import pytest

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.retrieval.orchestrator import RetrievalOrchestrator
from memory_app.schemas.retrieve import RetrieveMemRequest
from test_suite.e2e.helpers import wired_client


class _BoomChannel:
    channel_name: str

    def __init__(self, name: str) -> None:
        self.channel_name = name

    async def retrieve(self, query: str, ctx: RetrievalContext, k: int):
        raise PluginError(
            PluginErrorCategory.DEPENDENCY,
            f"{self.channel_name}_unavailable",
            f"{self.channel_name} down",
            retryable=True,
        )


class _OkChannel:
    def __init__(self, name: str) -> None:
        self.channel_name = name

    async def retrieve(self, query: str, ctx: RetrievalContext, k: int):
        return [
            RankedMemory(
                memory_id=f"id-{self.channel_name}",
                memory_type=MemoryType.EPISODIC,
                content=f"hit from {self.channel_name}",
                score=1.0,
                source_channel=self.channel_name,
                metadata={},
            )
        ]


@pytest.mark.e2e
class TestE2EDegradation:
    @pytest.mark.asyncio
    async def test_e2e_deg_001_es_down_vector_ok(self):
        from memory_app.retrieval.fusion import RRFFusion
        from memory_app.retrieval.reranker import MMRReranker

        orch = RetrievalOrchestrator(
            channels={"bm25": _BoomChannel("bm25"), "vector": _OkChannel("vector")},
            fuser=RRFFusion(),
            filters=[],
            reranker=MMRReranker(),
        )
        req = RetrieveMemRequest(tenant_id="t1", user_id="u1", query="test", top_k=5)
        result = await orch.execute(req)
        assert any(h.source_channel == "vector" for h in result)

    @pytest.mark.asyncio
    async def test_e2e_deg_002_all_channels_fail_retryable(self):
        from memory_app.retrieval.fusion import RRFFusion
        from memory_app.retrieval.reranker import MMRReranker

        orch = RetrievalOrchestrator(
            channels={
                "bm25": _BoomChannel("bm25"),
                "vector": _BoomChannel("vector"),
            },
            fuser=RRFFusion(),
            filters=[],
            reranker=MMRReranker(),
        )
        with pytest.raises(PluginError) as ei:
            await orch.execute(
                RetrieveMemRequest(tenant_id="t1", user_id="u1", query="q", top_k=3)
            )
        assert ei.value.code == "all_channels_failed"
        assert ei.value.retryable is True

    def test_e2e_deg_003_ingest_hot_path_when_cold_skipped(self):
        with wired_client() as (client, mongo, _es):
            r = client.post(
                "/v1/memory/ingest",
                json={
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "history_sessions": [
                        {
                            "session_id": "s1",
                            "turns": [{"role": "user", "content": "热路径写入"}],
                        }
                    ],
                },
            )
            assert r.status_code == 200
            assert mongo.store
