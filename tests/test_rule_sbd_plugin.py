"""``RuleSBD`` 插件 SPI 契约测试(Step 2.1)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.internal_models import RawData
from memory_app.plugins import registry as plugin_registry
from memory_app.plugins.spi.boundary_detector import (
    BoundaryContext,
    BoundaryDetectionResult,
    BoundaryDetector,
)


# ════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _ensure_plugins_loaded():
    """触发默认插件注册(rule_sbd 等)。"""
    import memory_app.plugins_default  # noqa: F401


def _raw(content: str, minutes_offset: int = 0) -> RawData:
    return RawData(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        content=content,
        event_time=datetime(2026, 1, 1, tzinfo=timezone.utc)
        + timedelta(minutes=minutes_offset),
    )


def _ctx() -> BoundaryContext:
    return BoundaryContext(
        tenant_id="t1",
        user_id="u1",
        current_time="2026-01-01T00:00:00Z",
    )


@pytest.fixture
def rule_sbd_class():
    """从 registry 取 RuleSBD 类(不直接 import plugins_default)。"""
    return plugin_registry.get("memory.generation.boundary_detector", "rule_sbd")


# ════════════════════════════════════════════════════════════════════════════
# 注册与元信息
# ════════════════════════════════════════════════════════════════════════════
class TestRegistration:
    def test_rule_sbd_registered(self, rule_sbd_class):
        assert issubclass(rule_sbd_class, BoundaryDetector)

    def test_meta_fields(self, rule_sbd_class):
        meta = rule_sbd_class.meta
        assert meta.name == "rule_sbd"
        assert meta.category == "memory.generation.boundary_detector"
        assert meta.version == "1.0.0"

    def test_config_schema_has_known_fields(self, rule_sbd_class):
        schema = rule_sbd_class.meta.config_schema
        props = schema["properties"]
        assert "time_gap_min" in props
        assert "max_window_size" in props
        assert "max_window_tokens" in props
        assert "llm_fallback" in props


# ════════════════════════════════════════════════════════════════════════════
# 生命周期
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestLifecycle:
    async def test_start_with_empty_config_uses_defaults(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({})
        assert sbd._config.time_gap_threshold == timedelta(minutes=30)
        assert sbd._config.max_window_turns == 20

    async def test_start_with_custom_config(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({"time_gap_min": 60, "max_window_size": 5})
        assert sbd._config.time_gap_threshold == timedelta(minutes=60)
        assert sbd._config.max_window_turns == 5

    async def test_stop_is_idempotent(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({})
        await sbd.stop()
        await sbd.stop()  # 再次 stop 不应抛

    async def test_health_returns_ok(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({"time_gap_min": 45})
        h = await sbd.health()
        assert h["status"] == "ok"
        assert "rule_sbd" in h["detail"]


# ════════════════════════════════════════════════════════════════════════════
# SPI: detect 单步判定
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestDetect:
    async def test_cold_start(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({})
        r = await sbd.detect(history=[], new=[_raw("hi")], ctx=_ctx())
        assert isinstance(r, BoundaryDetectionResult)
        assert r.should_end is False
        assert r.should_wait is False
        assert r.reasoning == "cold_start"

    async def test_within_window_no_split(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({})
        history = [_raw("a", 0)]
        new = [_raw("b", 5)]
        r = await sbd.detect(history=history, new=new, ctx=_ctx())
        assert r.should_end is False

    async def test_time_gap_triggers_split(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({"time_gap_min": 30})
        history = [_raw("a", 0)]
        new = [_raw("b", 60)]
        r = await sbd.detect(history=history, new=new, ctx=_ctx())
        assert r.should_end is True
        assert r.reasoning == "time_gap_exceeded"

    async def test_empty_new_does_not_crash(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({})
        r = await sbd.detect(history=[_raw("a", 0)], new=[], ctx=_ctx())
        assert r.should_end is False
        assert r.reasoning == "empty_new"


# ════════════════════════════════════════════════════════════════════════════
# 便利方法: segment 批量切分
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestSegment:
    async def test_segment_empty(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({})
        assert await sbd.segment([]) == []

    async def test_segment_continuous_returns_single(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({})
        raws = [_raw(f"m{i}", i) for i in range(5)]
        segs = await sbd.segment(raws)
        assert len(segs) == 1
        assert len(segs[0]) == 5

    async def test_segment_with_time_gap(self, rule_sbd_class):
        sbd = rule_sbd_class()
        await sbd.start({"time_gap_min": 30})
        raws = [_raw("a", 0), _raw("b", 60), _raw("c", 65)]
        segs = await sbd.segment(raws)
        assert len(segs) == 2
        assert len(segs[0]) == 1
        assert len(segs[1]) == 2

    async def test_segment_uses_runtime_config(self, rule_sbd_class):
        """不同 start 配置应影响后续 segment 行为(配置生效)。"""
        sbd = rule_sbd_class()
        await sbd.start({"max_window_size": 2})
        raws = [_raw(f"m{i}", i) for i in range(4)]
        segs = await sbd.segment(raws)
        assert len(segs) == 2  # 每 2 个切一段
