"""SBD LLM 模式 + HybridSBD 测试(Step 3.1)。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- :func:`memory_app.sbd.needs_llm_refinement` 启发式
- :func:`format_numbered_segments`            带行号格式化
- :func:`parse_llm_boundary_response`         JSON 解析容错
- :func:`split_segment_at`                    边界切分
- :class:`LLMSBD`                             纯 LLM 切分(独立测试)
- :class:`HybridSBD`                          规则优先 + LLM 兜底:
  - llm_fallback=False / llm_client=None → 等价规则
  - 短 segment 不触发 LLM
  - 长 segment 触发 LLM,正常返回 → 二次切分
  - LLM 异常 → 安全回退规则结果
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.internal_models import RawData
from memory_app.plugins_default.hybrid_sbd import HybridSBD
from memory_app.plugins_default.llm_sbd import LLMSBD
from memory_app.sbd import (
    LLM_REFINE_TURNS_THRESHOLD,
    format_numbered_segments,
    needs_llm_refinement,
    parse_llm_boundary_response,
    split_segment_at,
)


def _raw(content: str, minutes: int = 0) -> RawData:
    return RawData(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        content=content,
        event_time=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes),
    )


# ════════════════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════════════════
class TestNeedsLLMRefinement:
    def test_short_segments_no_refine(self):
        segs = [[_raw("a", i) for i in range(3)]]
        assert needs_llm_refinement(segs) is False

    def test_long_segment_triggers_refine(self):
        segs = [[_raw(f"m{i}", i) for i in range(LLM_REFINE_TURNS_THRESHOLD + 1)]]
        assert needs_llm_refinement(segs) is True

    def test_custom_threshold(self):
        segs = [[_raw(f"m{i}", i) for i in range(5)]]
        assert needs_llm_refinement(segs, turns_threshold=3) is True
        assert needs_llm_refinement(segs, turns_threshold=20) is False


class TestFormatNumberedSegments:
    def test_basic(self):
        segs = [_raw("hello"), _raw("world", 5)]
        out = format_numbered_segments(segs)
        assert out == "[0] hello\n[1] world"

    def test_replaces_newlines(self):
        segs = [_raw("a\nb"), _raw("c")]
        out = format_numbered_segments(segs)
        assert "\n" not in out.split("\n")[0]
        assert "[0] a b" in out


class TestParseLLMBoundaryResponse:
    def test_clean_json(self):
        idx, reason, conf = parse_llm_boundary_response(
            '{"boundary_index": 3, "reasoning": "shifted topic", "confidence": 0.8}'
        )
        assert idx == 3
        assert "shifted" in reason
        assert conf == 0.8

    def test_no_split_index_minus_1(self):
        idx, _, _ = parse_llm_boundary_response('{"boundary_index": -1}')
        assert idx == -1

    def test_markdown_code_fence(self):
        resp = "```json\n{\"boundary_index\": 2}\n```"
        idx, _, _ = parse_llm_boundary_response(resp)
        assert idx == 2

    def test_invalid_json(self):
        idx, reason, conf = parse_llm_boundary_response("not json")
        assert idx == -1 and reason == "parse_failed" and conf == 0.0

    def test_missing_field(self):
        idx, _, _ = parse_llm_boundary_response('{"reasoning": "x"}')
        assert idx == -1

    def test_empty(self):
        idx, _, _ = parse_llm_boundary_response("")
        assert idx == -1

    def test_embedded_in_text(self):
        resp = "Sure, here is the result: {\"boundary_index\": 5}. Hope it helps."
        idx, _, _ = parse_llm_boundary_response(resp)
        assert idx == 5


class TestSplitSegmentAt:
    def test_split_middle(self):
        segs = [_raw(f"m{i}", i) for i in range(5)]
        pieces = split_segment_at(segs, 2)
        assert [len(p) for p in pieces] == [2, 3]
        assert pieces[0][0].content == "m0"
        assert pieces[1][0].content == "m2"

    def test_no_split_when_idx_zero(self):
        segs = [_raw("a"), _raw("b", 5)]
        pieces = split_segment_at(segs, 0)
        assert len(pieces) == 1

    def test_no_split_when_idx_oob(self):
        segs = [_raw("a"), _raw("b", 5)]
        pieces = split_segment_at(segs, 99)
        assert len(pieces) == 1


# ════════════════════════════════════════════════════════════════════════════
# Mock LLM
# ════════════════════════════════════════════════════════════════════════════
class _FakeLLM:
    """计数 + 队列响应。"""

    def __init__(self, responses: list[str] | None = None, fail: bool = False):
        self._responses = list(responses or [])
        self.calls: list[str] = []
        self.fail = fail

    async def generate(self, prompt: str, **_) -> str:
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("LLM down")
        if not self._responses:
            return '{"boundary_index": -1}'
        return self._responses.pop(0)


# ════════════════════════════════════════════════════════════════════════════
# LLMSBD 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestLLMSBD:
    async def test_no_client_segment_returns_full(self):
        plugin = LLMSBD()
        await plugin.start({})
        segs = [_raw("a"), _raw("b")]
        out = await plugin.segment(segs)
        assert len(out) == 1 and len(out[0]) == 2

    async def test_with_client_splits_at_index(self):
        plugin = LLMSBD()
        await plugin.start({})
        plugin.bind_llm_client(_FakeLLM(['{"boundary_index": 2, "reasoning": "x", "confidence": 0.9}']))
        segs = [_raw(f"m{i}", i) for i in range(5)]
        out = await plugin.segment(segs)
        assert len(out) == 2

    async def test_short_input_skips_llm(self):
        plugin = LLMSBD()
        await plugin.start({})
        llm = _FakeLLM()
        plugin.bind_llm_client(llm)
        # 只有 1 条 → 直接返回
        out = await plugin.segment([_raw("only")])
        assert llm.calls == []
        assert len(out) == 1


# ════════════════════════════════════════════════════════════════════════════
# HybridSBD 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestHybridSBDRuleOnly:
    async def test_llm_fallback_disabled_acts_like_rule(self):
        plugin = HybridSBD()
        await plugin.start({"llm_fallback": False})
        # 不需要 client
        segs = [_raw(f"m{i}", i) for i in range(15)]  # 不到时间窗 / 无 turns 限
        out = await plugin.segment(segs)
        assert len(out) == 1

    async def test_llm_fallback_enabled_but_no_client(self):
        plugin = HybridSBD()
        await plugin.start({"llm_fallback": True})
        # 不绑 client → 实际仍走规则
        segs = [_raw(f"m{i}", i) for i in range(LLM_REFINE_TURNS_THRESHOLD + 5)]
        out = await plugin.segment(segs)
        assert len(out) == 1  # 规则窗口未撑爆

    async def test_short_segments_no_llm_call(self):
        plugin = HybridSBD()
        await plugin.start({"llm_fallback": True, "refine_threshold": 50})
        llm = _FakeLLM()
        plugin.bind_llm_client(llm)
        segs = [_raw(f"m{i}", i) for i in range(5)]
        await plugin.segment(segs)
        assert llm.calls == []  # 短 segment 不触发 LLM


@pytest.mark.asyncio
class TestHybridSBDLLMRefine:
    async def test_llm_split_long_segment(self):
        plugin = HybridSBD()
        await plugin.start({
            "llm_fallback": True,
            "refine_threshold": 5,
            "max_window_turns": 100,
        })
        plugin.bind_llm_client(
            _FakeLLM(['{"boundary_index": 4, "reasoning": "topic shift", "confidence": 0.85}'])
        )
        segs = [_raw(f"m{i}", i) for i in range(8)]
        out = await plugin.segment(segs)
        # LLM 在 idx=4 切一刀 → 2 段
        assert len(out) == 2
        assert [len(p) for p in out] == [4, 4]

    async def test_llm_no_split_returns_one(self):
        plugin = HybridSBD()
        await plugin.start({
            "llm_fallback": True,
            "refine_threshold": 5,
            "max_window_turns": 100,
        })
        plugin.bind_llm_client(_FakeLLM(['{"boundary_index": -1}']))
        segs = [_raw(f"m{i}", i) for i in range(8)]
        out = await plugin.segment(segs)
        assert len(out) == 1

    async def test_llm_failure_falls_back_to_rule(self):
        plugin = HybridSBD()
        await plugin.start({
            "llm_fallback": True,
            "refine_threshold": 5,
            "max_window_turns": 100,
        })
        plugin.bind_llm_client(_FakeLLM(fail=True))
        segs = [_raw(f"m{i}", i) for i in range(8)]
        # 不应抛
        out = await plugin.segment(segs)
        # 规则 fallback:整段一段
        assert len(out) == 1
        # 失败计数已 +1
        metrics = await plugin.metrics()
        assert metrics["hybrid_sbd_llm_failures"] >= 1

    async def test_llm_invalid_json_falls_back(self):
        plugin = HybridSBD()
        await plugin.start({
            "llm_fallback": True,
            "refine_threshold": 5,
            "max_window_turns": 100,
        })
        plugin.bind_llm_client(_FakeLLM(["not json at all"]))
        segs = [_raw(f"m{i}", i) for i in range(8)]
        out = await plugin.segment(segs)
        # parse_failed → idx=-1 → 不切
        assert len(out) == 1


@pytest.mark.asyncio
class TestHybridSBDDetect:
    async def test_detect_cold_start(self):
        plugin = HybridSBD()
        await plugin.start({})
        from memory_app.plugins.spi.boundary_detector import BoundaryContext

        ctx = BoundaryContext(tenant_id="t1", user_id="u1", current_time="2026-01-01")
        result = await plugin.detect([], [_raw("hi")], ctx)
        assert result.should_end is False
        assert result.reasoning == "cold_start"

    async def test_detect_time_gap(self):
        plugin = HybridSBD()
        await plugin.start({"time_gap_min": 30})
        from memory_app.plugins.spi.boundary_detector import BoundaryContext

        ctx = BoundaryContext(tenant_id="t1", user_id="u1", current_time="2026-01-01")
        history = [_raw("a", 0)]
        new = [_raw("b", 60)]  # 60min > 30min
        result = await plugin.detect(history, new, ctx)
        assert result.should_end is True
