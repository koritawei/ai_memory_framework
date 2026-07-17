"""SBD 纯规则算法测试(Step 2.1 / §5.1.2)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.internal_models import RawData
from memory_app.sbd import (
    SBDConfig,
    detect_boundaries,
    parse_sbd_config,
    should_split,
)


# ════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════
def _raw(content: str, minutes_offset: int = 0) -> RawData:
    return RawData(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        content=content,
        event_time=datetime(2026, 1, 1, tzinfo=timezone.utc)
        + timedelta(minutes=minutes_offset),
    )


# ════════════════════════════════════════════════════════════════════════════
# should_split 单步判定
# ════════════════════════════════════════════════════════════════════════════
class TestShouldSplit:
    def test_cold_start_returns_false(self):
        cfg = SBDConfig()
        end, reason = should_split([], _raw("hi"), cfg)
        assert end is False
        assert reason == "cold_start"

    def test_within_window(self):
        cfg = SBDConfig()
        cur = [_raw("a", 0)]
        end, reason = should_split(cur, _raw("b", 5), cfg)
        assert end is False
        assert reason == "within_window"

    def test_time_gap_exceeded(self):
        cfg = SBDConfig(time_gap_threshold=timedelta(minutes=30))
        cur = [_raw("a", 0)]
        end, reason = should_split(cur, _raw("b", 60), cfg)
        assert end is True
        assert reason == "time_gap_exceeded"

    def test_window_turns_reached(self):
        cfg = SBDConfig(max_window_turns=3)
        cur = [_raw(f"m{i}", i) for i in range(3)]
        end, reason = should_split(cur, _raw("m4", 4), cfg)
        assert end is True
        assert reason == "max_window_turns_reached"

    def test_window_tokens_reached(self):
        cfg = SBDConfig(max_window_tokens=10)
        # 字符 / 4 ≥ 10 → 总字符 ≥ 40 才命中
        # 让 token 先于 turns 触发,轮数压在 cap 之下
        cfg.max_window_turns = 100
        long_msg = "x" * 200  # 200/4 = 50 token
        cur = [_raw(long_msg, 0)]
        end, reason = should_split(cur, _raw("z", 5), cfg)
        assert end is True
        assert reason == "max_window_tokens_reached"

    def test_naive_datetime_treated_as_utc(self):
        """naive datetime 不应抛 TypeError(tz-naive vs tz-aware 比较)。"""
        cfg = SBDConfig()
        naive = RawData(
            tenant_id="t",
            user_id="u",
            session_id="s",
            content="x",
            event_time=datetime(2026, 1, 1),  # naive
        )
        aware = _raw("y", 60)
        # 不应抛
        end, _ = should_split([naive], aware, cfg)
        assert end is True


# ════════════════════════════════════════════════════════════════════════════
# detect_boundaries 批量切分
# ════════════════════════════════════════════════════════════════════════════
class TestDetectBoundaries:
    def test_empty_input(self):
        assert detect_boundaries([]) == []

    def test_single_input(self):
        segs = detect_boundaries([_raw("a", 0)])
        assert len(segs) == 1
        assert len(segs[0]) == 1

    def test_no_split_continuous(self):
        raws = [_raw("a", 0), _raw("b", 5), _raw("c", 10)]
        segs = detect_boundaries(raws)
        assert len(segs) == 1
        assert len(segs[0]) == 3

    def test_split_by_time_gap(self):
        raws = [_raw("a", 0), _raw("b", 60)]  # 60min > 30min 默认
        segs = detect_boundaries(raws)
        assert len(segs) == 2
        assert len(segs[0]) == 1
        assert len(segs[1]) == 1

    def test_split_by_window_turns(self):
        cfg = SBDConfig(max_window_turns=3)
        raws = [_raw(f"m{i}", i) for i in range(6)]  # 6 条,3 turn 切一次
        segs = detect_boundaries(raws, cfg)
        assert len(segs) == 2
        assert len(segs[0]) == 3
        assert len(segs[1]) == 3

    def test_multiple_splits(self):
        # 模拟多个时间段
        raws = [
            _raw("morning1", 0),
            _raw("morning2", 5),       # < 30min: same seg
            _raw("afternoon1", 100),   # 95min gap: new seg
            _raw("afternoon2", 110),   # < 30min: same seg
            _raw("evening", 250),      # 140min gap: new seg
        ]
        segs = detect_boundaries(raws)
        assert len(segs) == 3
        assert [len(s) for s in segs] == [2, 2, 1]

    def test_preserves_order(self):
        raws = [_raw(f"m{i}", i * 60) for i in range(3)]  # 都被切开
        segs = detect_boundaries(raws)
        # 顺序保持
        assert segs[0][0].content == "m0"
        assert segs[1][0].content == "m1"
        assert segs[2][0].content == "m2"


# ════════════════════════════════════════════════════════════════════════════
# parse_sbd_config 配置解析
# ════════════════════════════════════════════════════════════════════════════
class TestParseSbdConfig:
    def test_empty_returns_default(self):
        cfg = parse_sbd_config(None)
        assert cfg.time_gap_threshold == timedelta(minutes=30)
        assert cfg.max_window_turns == 20
        assert cfg.max_window_tokens == 512

    def test_time_gap_min_in_minutes(self):
        cfg = parse_sbd_config({"time_gap_min": 45})
        assert cfg.time_gap_threshold == timedelta(minutes=45)

    def test_max_window_turns_explicit(self):
        cfg = parse_sbd_config({"max_window_turns": 10})
        assert cfg.max_window_turns == 10

    def test_max_window_size_alias(self):
        # 兼容 noop_sbd 旧字段名
        cfg = parse_sbd_config({"max_window_size": 5})
        assert cfg.max_window_turns == 5

    def test_max_window_tokens(self):
        cfg = parse_sbd_config({"max_window_tokens": 1024})
        assert cfg.max_window_tokens == 1024

    def test_full_combination(self):
        cfg = parse_sbd_config(
            {"time_gap_min": 60, "max_window_size": 10, "max_window_tokens": 256}
        )
        assert cfg.time_gap_threshold == timedelta(minutes=60)
        assert cfg.max_window_turns == 10
        assert cfg.max_window_tokens == 256
