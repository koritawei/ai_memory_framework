"""BM25Channel + bm25_es 插件测试。"""

from __future__ import annotations

import pytest

from memory_app.internal_models import MemoryType
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.plugins_default.bm25_es_channel import BM25ESChannel
from memory_app.retrieval.channels.bm25 import BM25Channel


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeES:
    def __init__(self, response: dict | None = None, fail: bool = False):
        self.response = response or {"hits": {"hits": []}}
        self.fail = fail
        self.search_calls: list[dict] = []

    async def search(self, *, index: str, body: dict):
        self.search_calls.append({"index": index, "body": body})
        if self.fail:
            raise RuntimeError("ES down")
        return self.response


def _ctx() -> RetrievalContext:
    return RetrievalContext(tenant_id="t1", user_id="u1")


# ════════════════════════════════════════════════════════════════════════════
# 核心算法
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestBM25Channel:
    async def test_search_parses_results(self):
        es = _FakeES(
            response={
                "hits": {
                    "hits": [
                        {
                            "_id": "mc1",
                            "_score": 5.2,
                            "_source": {
                                "text": "北京出差",
                                "tenant_id": "t1",
                                "user_id": "u1",
                                "memory_type": "EPISODIC",
                            },
                        },
                        {
                            "_id": "mc2",
                            "_score": 3.1,
                            "_source": {
                                "text": "上海会议",
                                "tenant_id": "t1",
                                "user_id": "u1",
                            },
                        },
                    ]
                }
            }
        )
        ch = BM25Channel(es_client=es)
        hits = await ch.search("t1", "u1", "北京", top_k=5)
        assert len(hits) == 2
        # 排序 + rank
        assert hits[0].score > hits[1].score
        assert hits[0].rank == 0 and hits[1].rank == 1
        # source_channel
        assert all(h.source_channel == "bm25" for h in hits)
        assert hits[0].memory_type == MemoryType.EPISODIC

    async def test_empty_results(self):
        ch = BM25Channel(es_client=_FakeES())
        assert await ch.search("t1", "u1", "nope") == []

    async def test_empty_query_short_circuit(self):
        es = _FakeES()
        ch = BM25Channel(es_client=es)
        assert await ch.search("t1", "u1", "  ") == []
        assert es.search_calls == []  # 未触达 ES

    async def test_unset_client_raises_dependency(self):
        ch = BM25Channel(es_client=None)
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY
        assert exc.value.retryable is True

    async def test_es_failure_wrapped_as_dependency(self):
        ch = BM25Channel(es_client=_FakeES(fail=True))
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY
        assert exc.value.retryable is True

    async def test_filter_clause_includes_tenant_user(self):
        es = _FakeES()
        ch = BM25Channel(es_client=es)
        await ch.search("t1", "u1", "x")
        body = es.search_calls[0]["body"]
        filt = body["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "t1"}} in filt
        assert {"term": {"user_id": "u1"}} in filt

    async def test_over_fetch_factor_applied(self):
        es = _FakeES()
        ch = BM25Channel(es_client=es, over_fetch_factor=3)
        await ch.search("t1", "u1", "x", top_k=5)
        assert es.search_calls[0]["body"]["size"] == 15

    async def test_extra_filter_term_added(self):
        es = _FakeES()
        ch = BM25Channel(es_client=es)
        await ch.search("t1", "u1", "x", filters={"memory_type": "SEMANTIC"})
        body = es.search_calls[0]["body"]
        assert {"term": {"memory_type": "SEMANTIC"}} in body["query"]["bool"]["filter"]


# ════════════════════════════════════════════════════════════════════════════
# 插件层
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestBM25ESPlugin:
    async def test_unbound_raises(self):
        plugin = BM25ESChannel()
        await plugin.start({})
        with pytest.raises(PluginError):
            await plugin.retrieve("x", _ctx(), 5)

    async def test_bound_returns(self):
        plugin = BM25ESChannel()
        await plugin.start({"index_name": "test_idx"})
        plugin.bind_es_client(
            _FakeES(
                response={
                    "hits": {
                        "hits": [{"_id": "m1", "_score": 1.5, "_source": {"text": "x"}}]
                    }
                }
            )
        )
        hits = await plugin.retrieve("query", _ctx(), 5)
        assert len(hits) == 1
        assert hits[0].source_channel == "bm25"

    async def test_health(self):
        plugin = BM25ESChannel()
        await plugin.start({})
        h = await plugin.health()
        assert h["status"] == "degraded"
        plugin.bind_es_client(_FakeES())
        h2 = await plugin.health()
        assert h2["status"] == "ok"

    async def test_channel_name_property(self):
        plugin = BM25ESChannel()
        await plugin.start({})
        assert plugin.channel_name == "bm25"
