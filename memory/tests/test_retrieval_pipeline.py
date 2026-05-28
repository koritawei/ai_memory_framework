"""RetrievalPipeline + RetrievalOrchestrator 五阶段串联测试。"""

from __future__ import annotations

import pytest

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.pipelines.retrieval import (
    RetrievalPipeline,
    RetrievalPipelineContext,
)
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.retrieval.fusion import RRFFusion
from memory_app.retrieval.orchestrator import RetrievalOrchestrator
from memory_app.retrieval.reranker import MMRReranker
from memory_app.schemas.retrieve import RetrieveMemRequest, RetrievalConfig


def _hit(mem_id: str, score: float, source: str = "bm25") -> RankedMemory:
    return RankedMemory(
        memory_id=mem_id,
        memory_type=MemoryType.EPISODIC,
        content=mem_id,
        score=score,
        source_channel=source,
    )


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _StubChannel:
    def __init__(self, hits, *, fail=False):
        self.hits = hits
        self.fail = fail
        self.calls = 0

    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("channel failed")
        return list(self.hits)


class _StubFilter:
    def __init__(self, threshold=0.0, fail=False):
        self.threshold = threshold
        self.fail = fail

    async def filter(self, candidates, ctx):
        if self.fail:
            raise RuntimeError("filter failed")
        return [c for c in candidates if c.score >= self.threshold]


def _req(query="q", top_k=5, **kw) -> RetrieveMemRequest:
    base = dict(tenant_id="t1", user_id="u1", query=query, top_k=top_k)
    base.update(kw)
    return RetrieveMemRequest(**base)


# ════════════════════════════════════════════════════════════════════════════
# 完整管线
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestRetrievalPipeline:
    async def test_full_pipeline_happy_path(self):
        bm25 = _StubChannel([_hit("a", 5), _hit("b", 3)])
        vec = _StubChannel([_hit("a", 0.9, "vector"), _hit("c", 0.5, "vector")])
        pipe = RetrievalPipeline(
            channels={"bm25": bm25, "vector": vec},
            fuser=RRFFusion(),
            filters=[_StubFilter(threshold=0.0)],
            reranker=MMRReranker(),
        )
        out = await pipe.execute(_req())
        # 三个去重后的 ID
        assert {h.memory_id for h in out} == {"a", "b", "c"}
        assert all(h.rank is not None for h in out)

    async def test_top_k_truncation(self):
        bm25 = _StubChannel([_hit(f"m{i}", 10 - i) for i in range(8)])
        pipe = RetrievalPipeline(
            channels={"bm25": bm25},
            fuser=RRFFusion(),
            filters=[],
            reranker=MMRReranker(),
        )
        out = await pipe.execute(_req(top_k=3))
        assert len(out) == 3

    async def test_recall_k_uses_over_fetch(self):
        bm25 = _StubChannel([_hit("a", 1.0)])
        pipe = RetrievalPipeline(
            channels={"bm25": bm25},
            fuser=RRFFusion(),
            filters=[],
            reranker=MMRReranker(),
            over_fetch_factor=4,
        )
        ctx = await pipe.build_context(_req(top_k=3))
        assert ctx.recall_k == 12

    async def test_request_override_over_fetch(self):
        bm25 = _StubChannel([_hit("a", 1)])
        pipe = RetrievalPipeline(
            channels={"bm25": bm25},
            fuser=RRFFusion(),
            filters=[],
            reranker=MMRReranker(),
            over_fetch_factor=4,
        )
        req = _req(top_k=3, retrieval_config=RetrievalConfig(over_fetch_factor=2))
        ctx = await pipe.build_context(req)
        assert ctx.recall_k == 6

    async def test_partial_channel_failure_continues(self):
        bm25 = _StubChannel([_hit("a", 1.0)])
        vec = _StubChannel([], fail=True)
        pipe = RetrievalPipeline(
            channels={"bm25": bm25, "vector": vec},
            fuser=RRFFusion(),
            filters=[],
            reranker=MMRReranker(),
        )
        out = await pipe.execute(_req())
        assert len(out) == 1
        assert out[0].memory_id == "a"

    async def test_all_channels_fail_raises(self):
        bm25 = _StubChannel([], fail=True)
        vec = _StubChannel([], fail=True)
        pipe = RetrievalPipeline(
            channels={"bm25": bm25, "vector": vec},
            fuser=RRFFusion(),
            filters=[],
            reranker=MMRReranker(),
        )
        with pytest.raises(PluginError) as exc:
            await pipe.execute(_req())
        assert exc.value.category == PluginErrorCategory.DEPENDENCY

    async def test_no_channels_returns_empty(self):
        pipe = RetrievalPipeline(channels={}, fuser=RRFFusion())
        out = await pipe.execute(_req())
        assert out == []

    async def test_no_fuser_falls_back_to_concat(self):
        bm25 = _StubChannel([_hit("a", 5)])
        vec = _StubChannel([_hit("b", 0.5, "vector")])
        pipe = RetrievalPipeline(channels={"bm25": bm25, "vector": vec}, fuser=None)
        out = await pipe.execute(_req())
        ids = {h.memory_id for h in out}
        assert ids == {"a", "b"}

    async def test_filter_failure_recorded_but_continues(self):
        bm25 = _StubChannel([_hit("a", 0.6)])
        pipe = RetrievalPipeline(
            channels={"bm25": bm25},
            fuser=RRFFusion(),
            filters=[_StubFilter(fail=True)],
            reranker=MMRReranker(),
        )
        # 不应抛
        out = await pipe.execute(_req())
        assert len(out) == 1

    async def test_filter_drops_low_score(self):
        bm25 = _StubChannel([_hit("a", 0.6), _hit("b", 0.1)])
        # 不用 fuser,直接 score 进 filter
        # 但 RRFFusion 会重写 score → 我们自己注入个 dummy fuser
        pipe = RetrievalPipeline(
            channels={"bm25": bm25},
            fuser=None,
            filters=[_StubFilter(threshold=0.5)],
            reranker=MMRReranker(),
        )
        out = await pipe.execute(_req())
        ids = [h.memory_id for h in out]
        assert ids == ["a"]

    async def test_enabled_channels_override(self):
        bm25 = _StubChannel([_hit("a", 1)])
        vec = _StubChannel([_hit("b", 1, "vector")])
        pipe = RetrievalPipeline(channels={"bm25": bm25, "vector": vec}, fuser=RRFFusion())
        req = _req(retrieval_config=RetrievalConfig(enabled_channels=["bm25"]))
        await pipe.execute(req)
        assert bm25.calls == 1
        assert vec.calls == 0


# ════════════════════════════════════════════════════════════════════════════
# RetrievalOrchestrator
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestRetrievalOrchestrator:
    async def test_retrieve_method(self):
        bm25 = _StubChannel([_hit("a", 1)])
        orch = RetrievalOrchestrator(
            channels={"bm25": bm25},
            fuser=RRFFusion(),
            filters=[],
            reranker=MMRReranker(),
        )
        out = await orch.retrieve(_req())
        assert len(out) == 1
