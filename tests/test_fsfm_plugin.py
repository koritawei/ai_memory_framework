"""FSFM4DScorer + EbbinghausPolicy 插件测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.internal_models import (
    EpisodicMemory,
    MemoryState,
    SemanticMemory,
)
from memory_app.plugins.spi.forgetting_policy import MemoryRef
from memory_app.plugins_default.ebbinghaus_policy import EbbinghausPolicy
from memory_app.plugins_default.fsfm_scorer import FSFM4DScorer


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
# FSFM4DScorer
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestFSFM4DScorer:
    async def test_score_ref(self):
        scorer = FSFM4DScorer()
        await scorer.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=2.0, access_count=3,
            created_at=_now() - timedelta(days=10),
        )
        result = await scorer.score_ref(ref)
        assert 0.0 <= result.composite <= 1.0
        # access_count*0.2 + strength*0.1 = 0.8
        assert result.bve == pytest.approx(0.8, rel=1e-2)

    async def test_score_episodic(self):
        scorer = FSFM4DScorer()
        await scorer.start({})
        mem = EpisodicMemory(
            episode_id="e1", mem_cell_id="mc1",
            tenant_id="t1", user_id="u1",
            summary="x" * 200,  # 文本足够长
            strength=1.0, access_count=2,
            created_at=_now(),
        )
        result = await scorer.score_episodic(mem)
        assert result.composite > 0.3

    async def test_score_semantic(self):
        scorer = FSFM4DScorer()
        await scorer.start({})
        mem = SemanticMemory(
            tenant_id="t1", user_id="u1",
            content="重要的事实",
            source_episode_ids=["e1", "e2"],
            source_memcell_ids=["mc1"],
            strength=1.0, access_count=1,
            created_at=_now(),
        )
        result = await scorer.score_semantic(mem)
        # 三个来源 → SRC = 3 × 0.3 = 0.9
        assert result.src == pytest.approx(0.9, rel=1e-2)

    async def test_score_cell_helper(self):
        scorer = FSFM4DScorer()
        await scorer.start({})
        from memory_app.internal_models import MemCell

        cell = MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="x" * 500, strength=2.0, access_count=3,
            raw_data_ids=["r1", "r2"],
        )
        s = scorer.score_cell(cell, now=_now())
        assert 0.0 <= s <= 1.0
        d = scorer.detail(cell, now=_now())
        assert "composite" in d

    async def test_health(self):
        scorer = FSFM4DScorer()
        await scorer.start({"w_cqa": 0.5})
        h = await scorer.health()
        assert h["status"] == "ok"
        assert "cqa=0.5" in h["detail"]

    async def test_config_overrides_weights(self):
        scorer = FSFM4DScorer()
        await scorer.start({"w_cqa": 1.0, "w_bve": 0.0, "w_trs": 0.0, "w_src": 0.0})
        from memory_app.internal_models import MemCell

        cell = MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="x" * 500, access_count=10, strength=5,
            created_at=_now() - timedelta(days=1000),
        )
        # w_cqa=1.0 其他 0 → composite ≈ cqa(1.0)
        s = scorer.score_cell(cell, now=_now())
        assert s == pytest.approx(1.0, abs=0.01)


# ════════════════════════════════════════════════════════════════════════════
# EbbinghausPolicy
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestEbbinghausPolicy:
    async def test_retention_score_recent(self):
        p = EbbinghausPolicy()
        await p.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=1.0, access_count=0,
            created_at=_now(),
        )
        score = await p.retention_score(ref, _now())
        # 刚创建,应有较高保留度
        assert 0.5 < score <= 1.0

    async def test_retention_score_old(self):
        p = EbbinghausPolicy()
        await p.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=0.5, access_count=0,
            created_at=_now() - timedelta(days=30),
        )
        score = await p.retention_score(ref, _now())
        # 30 天 + strength 0.5 → 应低
        assert score < 0.3

    async def test_high_access_keeps_score(self):
        p = EbbinghausPolicy()
        await p.start({})
        old = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=1.0, access_count=0,
            created_at=_now() - timedelta(days=10),
        )
        hot = MemoryRef(
            memory_id="m2", memory_type="EPISODIC",
            strength=1.0, access_count=20,
            created_at=_now() - timedelta(days=10),
        )
        s_old = await p.retention_score(old, _now())
        s_hot = await p.retention_score(hot, _now())
        assert s_hot > s_old

    async def test_step_returns_new_list(self):
        p = EbbinghausPolicy()
        await p.start({})
        refs = [
            MemoryRef(
                memory_id=f"m{i}", memory_type="EPISODIC",
                strength=1.0, access_count=0, created_at=_now(),
            )
            for i in range(3)
        ]
        out = await p.step(refs, dt_seconds=86400.0)
        assert len(out) == 3
        # 新对象,不修改入参
        assert out[0] is not refs[0]

    async def test_health(self):
        p = EbbinghausPolicy()
        await p.start({"s_base": 7.0})
        h = await p.health()
        assert h["status"] == "ok"
        assert "s_base=7.0" in h["detail"]

    async def test_threshold_forget_config(self):
        p = EbbinghausPolicy()
        await p.start({"threshold_forget": 0.05})
        assert p._config.threshold_forget == 0.05
