"""VectorChannel + vector_milvus 插件测试。"""

from __future__ import annotations

import pytest

from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.plugins_default.vector_milvus_channel import VectorMilvusChannel
from memory_app.retrieval.channels.vector import VectorChannel


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeEntity:
    def __init__(self, fields: dict):
        self._fields = fields

    def get(self, k, default=None):
        return self._fields.get(k, default)


class _FakeHit:
    def __init__(self, mem_id: str, distance: float, fields: dict):
        self.id = mem_id
        self.distance = distance
        self.entity = _FakeEntity({**fields, "mem_cell_id": mem_id})


class _FakeMilvusCollection:
    def __init__(self, hits=None, fail=False):
        self._hits = hits or []
        self.fail = fail
        self.search_calls: list[dict] = []

    def search(self, *, data, anns_field, param, limit, expr, output_fields):
        self.search_calls.append(
            {
                "data": data, "anns_field": anns_field, "param": param,
                "limit": limit, "expr": expr, "output_fields": output_fields,
            }
        )
        if self.fail:
            raise RuntimeError("milvus down")
        return [self._hits]


class _FakeEmbedding:
    def __init__(self, vector=None, fail=False):
        self.vector = vector or [0.1] * 8
        self.fail = fail
        self.embed_calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embed down")
        return [list(self.vector) for _ in texts]


def _ctx() -> RetrievalContext:
    return RetrievalContext(tenant_id="t1", user_id="u1")


# ════════════════════════════════════════════════════════════════════════════
# 核心算法
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestVectorChannel:
    async def test_search_parses_hits(self):
        col = _FakeMilvusCollection(
            hits=[
                _FakeHit("mc1", 0.92, {"text": "北京", "memory_type": "EPISODIC"}),
                _FakeHit("mc2", 0.81, {"text": "上海"}),
            ]
        )
        ch = VectorChannel(collection=col, embedding_client=_FakeEmbedding())
        hits = await ch.search("t1", "u1", "query")
        assert len(hits) == 2
        assert hits[0].score >= hits[1].score
        assert all(h.source_channel == "vector" for h in hits)
        assert hits[0].rank == 0

    async def test_unset_collection_raises(self):
        ch = VectorChannel(collection=None, embedding_client=_FakeEmbedding())
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY

    async def test_unset_embedding_raises(self):
        ch = VectorChannel(collection=_FakeMilvusCollection(), embedding_client=None)
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY

    async def test_embedding_failure_wrapped(self):
        ch = VectorChannel(
            collection=_FakeMilvusCollection(),
            embedding_client=_FakeEmbedding(fail=True),
        )
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY

    async def test_milvus_failure_wrapped(self):
        ch = VectorChannel(
            collection=_FakeMilvusCollection(fail=True),
            embedding_client=_FakeEmbedding(),
        )
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY

    async def test_filters_in_expr(self):
        col = _FakeMilvusCollection()
        ch = VectorChannel(collection=col, embedding_client=_FakeEmbedding())
        await ch.search("t1", "u1", "x", filters={"memory_type": "SEMANTIC"})
        expr = col.search_calls[0]["expr"]
        assert 'tenant_id == "t1"' in expr
        assert 'user_id == "u1"' in expr
        assert 'memory_type == "SEMANTIC"' in expr

    async def test_over_fetch_factor(self):
        col = _FakeMilvusCollection()
        ch = VectorChannel(
            collection=col, embedding_client=_FakeEmbedding(), over_fetch_factor=3
        )
        await ch.search("t1", "u1", "x", top_k=4)
        assert col.search_calls[0]["limit"] == 12

    async def test_empty_milvus_returns_empty(self):
        ch = VectorChannel(
            collection=_FakeMilvusCollection(hits=[]),
            embedding_client=_FakeEmbedding(),
        )
        assert await ch.search("t1", "u1", "q") == []


# ════════════════════════════════════════════════════════════════════════════
# 插件层
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestVectorMilvusPlugin:
    async def test_unbound_raises(self):
        plugin = VectorMilvusChannel()
        await plugin.start({})
        with pytest.raises(PluginError):
            await plugin.retrieve("q", _ctx(), 5)

    async def test_bound_returns(self):
        plugin = VectorMilvusChannel()
        await plugin.start({})
        plugin.bind_collection(
            _FakeMilvusCollection(hits=[_FakeHit("mc1", 0.5, {"text": "x"})])
        )
        plugin.bind_embedding_client(_FakeEmbedding())
        hits = await plugin.retrieve("q", _ctx(), 5)
        assert len(hits) == 1
        assert hits[0].source_channel == "vector"

    async def test_health(self):
        plugin = VectorMilvusChannel()
        await plugin.start({})
        h = await plugin.health()
        assert h["status"] == "degraded"
        plugin.bind_collection(_FakeMilvusCollection())
        plugin.bind_embedding_client(_FakeEmbedding())
        h2 = await plugin.health()
        assert h2["status"] == "ok"

    async def test_channel_name(self):
        plugin = VectorMilvusChannel()
        await plugin.start({})
        assert plugin.channel_name == "vector"

    async def test_metric_and_nprobe_config(self):
        plugin = VectorMilvusChannel()
        await plugin.start({"metric_type": "IP", "nprobe": 32})
        col = _FakeMilvusCollection()
        plugin.bind_collection(col)
        plugin.bind_embedding_client(_FakeEmbedding())
        await plugin.retrieve("q", _ctx(), 5)
        assert col.search_calls[0]["param"]["metric_type"] == "IP"
        assert col.search_calls[0]["param"]["params"]["nprobe"] == 32
