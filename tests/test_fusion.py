"""RRF 融合 + 信号增强测试。"""

from __future__ import annotations

import pytest

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins_default.weighted_rrf_fuser import WeightedRRFFuser
from memory_app.retrieval.fusion import (
    RRFConfig,
    RRFFusion,
    SignalBoost,
    parse_rrf_config,
)


def _hit(mem_id: str, score: float, source: str = "bm25") -> RankedMemory:
    return RankedMemory(
        memory_id=mem_id,
        memory_type=MemoryType.EPISODIC,
        content=mem_id,
        score=score,
        source_channel=source,
    )


# ════════════════════════════════════════════════════════════════════════════
# RRFConfig
# ════════════════════════════════════════════════════════════════════════════
class TestRRFConfig:
    def test_default_weights(self):
        cfg = RRFConfig()
        assert cfg.weight("bm25") == 0.30
        assert cfg.weight("vector") == 0.40
        assert cfg.weight("entity") == 0.15
        assert cfg.weight("graph") == 0.15
        assert cfg.weight("unknown") == 0.0

    def test_parse_from_params(self):
        cfg = parse_rrf_config({"k": 30, "weights": {"bm25": 0.5}})
        assert cfg.k == 30
        assert cfg.weight("bm25") == 0.5
        # 未覆盖的不变
        assert cfg.weight("vector") == 0.40

    def test_parse_invalid_weight_skipped(self):
        cfg = parse_rrf_config({"weights": {"bm25": "not-a-number"}})
        assert cfg.weight("bm25") == 0.30  # 保留默认


# ════════════════════════════════════════════════════════════════════════════
# RRFFusion
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestRRFFusion:
    async def test_basic_fusion_dedup(self):
        fusion = RRFFusion()
        bm25 = [_hit("mc1", 5.0), _hit("mc2", 3.0)]
        vec = [_hit("mc2", 0.9, "vector"), _hit("mc3", 0.8, "vector")]
        out = await fusion.fuse({"bm25": bm25, "vector": vec})
        ids = {h.memory_id for h in out}
        # 去重
        assert ids == {"mc1", "mc2", "mc3"}
        # mc2 同时出现在两路 → 分数最高
        assert out[0].memory_id == "mc2"

    async def test_rank_filled(self):
        fusion = RRFFusion()
        out = await fusion.fuse(
            {"bm25": [_hit("a", 1.0), _hit("b", 0.5)]}
        )
        assert out[0].rank == 0 and out[1].rank == 1

    async def test_rrf_formula(self):
        # 单路 bm25,k=60,w_bm25=0.30:
        # rank=0 → 0.30/61 ≈ 0.004918
        # rank=1 → 0.30/62 ≈ 0.004839
        fusion = RRFFusion()
        out = await fusion.fuse({"bm25": [_hit("a", 100), _hit("b", 50)]})
        assert pytest.approx(out[0].score, rel=1e-3) == 0.30 / 61
        assert pytest.approx(out[1].score, rel=1e-3) == 0.30 / 62

    async def test_zero_weight_channel_skipped(self):
        fusion = RRFFusion(config=RRFConfig(weights={"bm25": 0.0, "vector": 1.0}))
        out = await fusion.fuse(
            {"bm25": [_hit("a", 5)], "vector": [_hit("b", 0.5, "vector")]}
        )
        ids = {h.memory_id for h in out}
        assert ids == {"b"}  # bm25 权重 0 → 不参与

    async def test_empty_input(self):
        fusion = RRFFusion()
        assert await fusion.fuse({}) == []

    async def test_doesnt_mutate_input(self):
        fusion = RRFFusion()
        h = _hit("mc1", 5.0)
        original_score = h.score
        await fusion.fuse({"bm25": [h]})
        assert h.score == original_score

    async def test_matched_channels_metadata(self):
        fusion = RRFFusion()
        out = await fusion.fuse(
            {
                "bm25": [_hit("mc1", 5.0)],
                "vector": [_hit("mc1", 0.9, "vector")],
            }
        )
        assert out[0].memory_id == "mc1"
        assert set(out[0].metadata["matched_channels"]) == {"bm25", "vector"}

    async def test_weights_override(self):
        fusion = RRFFusion()
        # 覆盖让 bm25 权重远大
        out = await fusion.fuse(
            {
                "bm25": [_hit("a", 5)],
                "vector": [_hit("b", 0.5, "vector")],
            },
            weights={"bm25": 1.0, "vector": 0.001},
        )
        assert out[0].memory_id == "a"


# ════════════════════════════════════════════════════════════════════════════
# 信号增强
# ════════════════════════════════════════════════════════════════════════════
class TestSignalBoost:
    def test_factor_for_default(self):
        sb = SignalBoost()
        assert sb.factor_for("any") == 1.0  # td=1, imp=0

    def test_factor_for_combined(self):
        sb = SignalBoost(time_decays={"a": 0.5}, importances={"a": 0.8})
        # 0.5 × (1 + 0.8) = 0.9
        assert sb.factor_for("a") == pytest.approx(0.9)


class TestApplySignalBoost:
    def test_changes_ranking(self):
        fusion = RRFFusion()
        hits = [_hit("a", 0.5), _hit("b", 0.4)]
        out = fusion.apply_signal_boost(
            hits,
            time_decays={"a": 0.5, "b": 1.0},  # a 衰减
            importances={"a": 0.0, "b": 0.8},  # b 高重要性
        )
        # a: 0.5 * 0.5 * 1.0 = 0.25
        # b: 0.4 * 1.0 * 1.8 = 0.72
        assert out[0].memory_id == "b"
        assert out[1].memory_id == "a"
        assert out[0].rank == 0

    def test_missing_factors_use_default(self):
        fusion = RRFFusion()
        h = _hit("a", 0.5)
        out = fusion.apply_signal_boost([h])  # 无 time_decays/importances
        # 不增强,score 不变
        assert out[0].score == 0.5


# ════════════════════════════════════════════════════════════════════════════
# WeightedRRFFuser 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestWeightedRRFFuserPlugin:
    async def test_fuse_via_plugin(self):
        plugin = WeightedRRFFuser()
        await plugin.start({})
        out = await plugin.fuse(
            {"bm25": [_hit("a", 5)], "vector": [_hit("a", 0.9, "vector")]}
        )
        assert out[0].memory_id == "a"

    async def test_apply_signal_boost_via_plugin(self):
        plugin = WeightedRRFFuser()
        await plugin.start({})
        out = plugin.apply_signal_boost(
            [_hit("a", 0.5), _hit("b", 0.4)],
            time_decays={"a": 0.5, "b": 1.0},
            importances={"a": 0.0, "b": 0.8},
        )
        assert out[0].memory_id == "b"

    async def test_config_loaded(self):
        plugin = WeightedRRFFuser()
        await plugin.start({"k": 30, "weights": {"bm25": 0.6}})
        h = await plugin.health()
        assert "k=30" in h["detail"]
