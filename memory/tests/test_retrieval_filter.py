"""ThresholdFilter 测试。"""

from __future__ import annotations

import pytest

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.plugins_default.threshold_filter import ThresholdFilter


def _hit(mem_id: str, score: float) -> RankedMemory:
    return RankedMemory(
        memory_id=mem_id,
        memory_type=MemoryType.EPISODIC,
        content=mem_id,
        score=score,
    )


def _ctx() -> RetrievalContext:
    return RetrievalContext(tenant_id="t1", user_id="u1")


@pytest.mark.asyncio
class TestThresholdFilter:
    async def test_default_threshold_055(self):
        plugin = ThresholdFilter()
        await plugin.start({})
        out = await plugin.filter(
            [_hit("a", 0.9), _hit("b", 0.6), _hit("c", 0.3)], _ctx()
        )
        ids = [h.memory_id for h in out]
        assert ids == ["a", "b"]

    async def test_custom_threshold(self):
        plugin = ThresholdFilter()
        await plugin.start({"threshold": 0.7})
        out = await plugin.filter([_hit("a", 0.9), _hit("b", 0.6)], _ctx())
        assert [h.memory_id for h in out] == ["a"]

    async def test_zero_threshold_keeps_all(self):
        plugin = ThresholdFilter()
        await plugin.start({"threshold": 0.0})
        out = await plugin.filter([_hit("a", 0.0)], _ctx())
        assert len(out) == 1

    async def test_invalid_threshold_falls_back(self):
        plugin = ThresholdFilter()
        await plugin.start({"threshold": "invalid"})
        # fallback 到默认 0.55
        out = await plugin.filter([_hit("a", 0.6), _hit("b", 0.4)], _ctx())
        assert [h.memory_id for h in out] == ["a"]

    async def test_clamp_threshold(self):
        plugin = ThresholdFilter()
        await plugin.start({"threshold": 5.0})
        # clamp 到 1.0,所有都会被过滤(score < 1.0)
        out = await plugin.filter([_hit("a", 0.99)], _ctx())
        assert out == []

    async def test_empty_input(self):
        plugin = ThresholdFilter()
        await plugin.start({})
        assert await plugin.filter([], _ctx()) == []

    async def test_preserves_order(self):
        plugin = ThresholdFilter()
        await plugin.start({"threshold": 0.0})
        hits = [_hit("c", 0.6), _hit("a", 0.9), _hit("b", 0.7)]
        out = await plugin.filter(hits, _ctx())
        # 不重排,保持入参顺序
        assert [h.memory_id for h in out] == ["c", "a", "b"]

    async def test_health(self):
        plugin = ThresholdFilter()
        await plugin.start({"threshold": 0.4})
        h = await plugin.health()
        assert h["status"] == "ok"
        assert "0.400" in h["detail"]
