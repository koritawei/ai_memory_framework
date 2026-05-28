"""scoring.py 工具函数测试(反馈与生命周期 + 5.3)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.internal_models import MemCell
from memory_app.schemas.feedback import FeedbackType
from memory_app.scoring import (
    EbbinghausConfig,
    FSFMConfig,
    FSFMScorer,
    ReinforceConfig,
    compute_strength_delta,
    ebbinghaus_retention,
    parse_ebbinghaus_config,
    parse_fsfm_config,
    parse_reinforce_config,
    resolve_signal,
)


def _now() -> datetime:
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def _cell(text="test", strength=1.0, access_count=0, *, raw_data_ids=None, days_ago=0.0) -> MemCell:
    return MemCell(
        tenant_id="t1", user_id="u1", session_id="s1",
        text=text, strength=strength, access_count=access_count,
        raw_data_ids=list(raw_data_ids or []),
        created_at=_now() - timedelta(days=days_ago),
    )


# ════════════════════════════════════════════════════════════════════════════
# 反馈强化
# ════════════════════════════════════════════════════════════════════════════
class TestReinforceConfig:
    def test_defaults(self):
        cfg = ReinforceConfig()
        assert cfg.eta == 0.3
        assert cfg.lambda_per_day == 0.01
        assert cfg.s_max == 5.0
        assert cfg.default_signals[FeedbackType.POSITIVE] == 0.3
        assert cfg.default_signals[FeedbackType.NEGATIVE] == -0.5

    def test_parse(self):
        cfg = parse_reinforce_config({"eta": 0.5, "s_max": 10.0})
        assert cfg.eta == 0.5
        assert cfg.s_max == 10.0

    def test_parse_default_signals(self):
        cfg = parse_reinforce_config({"default_signals": {"positive": 0.7}})
        assert cfg.default_signals[FeedbackType.POSITIVE] == 0.7

    def test_invalid_default_signal_skipped(self):
        cfg = parse_reinforce_config({"default_signals": {"unknown_type": 1.0}})
        assert FeedbackType.POSITIVE in cfg.default_signals


class TestResolveSignal:
    def test_zero_uses_default(self):
        cfg = ReinforceConfig()
        assert resolve_signal(FeedbackType.POSITIVE, 0.0, cfg) == 0.3
        assert resolve_signal(FeedbackType.EXPLICIT_CONFIRM, 0.0, cfg) == 1.0
        assert resolve_signal(FeedbackType.NEGATIVE, 0.0, cfg) == -0.5

    def test_explicit_overrides(self):
        cfg = ReinforceConfig()
        assert resolve_signal(FeedbackType.POSITIVE, 0.8, cfg) == 0.8


class TestComputeStrengthDelta:
    def test_positive_increases(self):
        new, delta = compute_strength_delta(
            old_strength=1.0, signal=0.3, last_at=None, now=_now(),
            config=ReinforceConfig(),
        )
        assert new == pytest.approx(1.09)
        assert delta == pytest.approx(0.09)

    def test_negative_decreases(self):
        new, delta = compute_strength_delta(
            old_strength=1.0, signal=-0.5, last_at=None, now=_now(),
            config=ReinforceConfig(),
        )
        assert new == pytest.approx(0.85)
        assert delta == pytest.approx(-0.15)

    def test_clamp_to_zero(self):
        new, _ = compute_strength_delta(
            old_strength=0.5, signal=-10.0, last_at=None, now=_now(),
            config=ReinforceConfig(),
        )
        assert new == 0.0

    def test_clamp_to_s_max(self):
        new, _ = compute_strength_delta(
            old_strength=4.9, signal=10.0, last_at=None, now=_now(),
            config=ReinforceConfig(s_max=5.0),
        )
        assert new == 5.0

    def test_time_decay_applied(self):
        new, _ = compute_strength_delta(
            old_strength=2.0, signal=0.3,
            last_at=_now() - timedelta(days=100), now=_now(),
            config=ReinforceConfig(),
        )
        # 2.0 + 0.09 - 1.0 = 1.09
        assert new == pytest.approx(1.09)


# ════════════════════════════════════════════════════════════════════════════
# FSFM
# ════════════════════════════════════════════════════════════════════════════
class TestFSFMScorer:
    def test_score_in_unit_interval(self):
        scorer = FSFMScorer()
        cell = _cell(text="x" * 600, strength=3.0, access_count=5,
                     raw_data_ids=["r1", "r2", "r3"], days_ago=0)
        s = scorer.score(cell, now=_now())
        assert 0.0 <= s <= 1.0
        assert s > 0.6

    def test_old_low_quality_low_score(self):
        scorer = FSFMScorer()
        cell = _cell(text="短", strength=0.1, access_count=0, days_ago=200)
        s = scorer.score(cell, now=_now())
        assert s < 0.3

    def test_cqa_text_length(self):
        assert FSFMScorer.cqa_score(_cell(text="a" * 100)) == pytest.approx(0.2)
        assert FSFMScorer.cqa_score(_cell(text="a" * 600)) == 1.0

    def test_bve_combines(self):
        s = FSFMScorer.bve_score(_cell(access_count=3, strength=2.0))
        assert s == pytest.approx(0.8)

    def test_trs_decays_over_time(self):
        scorer = FSFMScorer()
        new = scorer.trs_score(_cell(days_ago=0), _now())
        old = scorer.trs_score(_cell(days_ago=60), _now())
        assert new > old
        assert new == pytest.approx(1.0)
        assert old == pytest.approx(0.25, rel=1e-2)

    def test_src_counts_raw_data(self):
        assert FSFMScorer.src_score(_cell(raw_data_ids=["r1"])) == pytest.approx(0.3)
        assert FSFMScorer.src_score(_cell(raw_data_ids=["r1", "r2", "r3", "r4"])) == 1.0

    def test_detail_breakdown(self):
        scorer = FSFMScorer()
        d = scorer.detail(_cell(text="x" * 100, strength=1, access_count=1, raw_data_ids=["r"]))
        assert set(d.keys()) >= {"cqa", "bve", "trs", "src", "composite"}

    def test_parse_config(self):
        cfg = parse_fsfm_config({"w_cqa": 0.5})
        assert cfg.w_cqa == 0.5


# ════════════════════════════════════════════════════════════════════════════
# Ebbinghaus
# ════════════════════════════════════════════════════════════════════════════
class TestEbbinghaus:
    def test_default_config(self):
        cfg = EbbinghausConfig()
        assert cfg.s_base == 4.0
        assert cfg.threshold_forget == 0.15

    def test_parse(self):
        cfg = parse_ebbinghaus_config({"s_base": 7.0})
        assert cfg.s_base == 7.0

    def test_retention_decays_over_time(self):
        cfg = EbbinghausConfig()
        new = ebbinghaus_retention(age_days=0, strength=1.0, access_count=0, config=cfg)
        old = ebbinghaus_retention(age_days=30, strength=1.0, access_count=0, config=cfg)
        assert new > old
        assert 0 <= new <= 1
        assert 0 <= old <= 1

    def test_high_access_boosts(self):
        cfg = EbbinghausConfig()
        low = ebbinghaus_retention(age_days=10, strength=1.0, access_count=0, config=cfg)
        high = ebbinghaus_retention(age_days=10, strength=1.0, access_count=20, config=cfg)
        assert high > low

    def test_high_strength_extends_half_life(self):
        cfg = EbbinghausConfig()
        weak = ebbinghaus_retention(age_days=10, strength=0.5, access_count=0, config=cfg)
        strong = ebbinghaus_retention(age_days=10, strength=3.0, access_count=0, config=cfg)
        assert strong > weak

    def test_negative_age_treated_zero(self):
        cfg = EbbinghausConfig()
        a = ebbinghaus_retention(age_days=-5, strength=1.0, access_count=0, config=cfg)
        b = ebbinghaus_retention(age_days=0, strength=1.0, access_count=0, config=cfg)
        assert a == b
