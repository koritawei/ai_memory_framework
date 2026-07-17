"""MMRReranker + mmr 插件测试(Step 4.4)。"""

from __future__ import annotations

import pytest

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins_default.mmr_reranker import MMRRerankerPlugin
from memory_app.retrieval.reranker import (
    MMRConfig,
    MMRReranker,
    cosine_similarity,
    parse_mmr_config,
)


def _hit(mem_id: str, score: float, *, embedding: list[float] | None = None) -> RankedMemory:
    md = {}
    if embedding is not None:
        md["embedding"] = embedding
    return RankedMemory(
        memory_id=mem_id,
        memory_type=MemoryType.EPISODIC,
        content=mem_id,
        score=score,
        metadata=md,
    )


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
class TestCosineSimilarity:
    def test_identical(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_dim_mismatch(self):
        assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0


class TestParseMMRConfig:
    def test_default(self):
        cfg = parse_mmr_config(None)
        assert cfg.mmr_lambda == 0.7
        assert cfg.enable_cross_encoder is False

    def test_custom(self):
        cfg = parse_mmr_config({"mmr_lambda": 0.5, "enable_cross_encoder": True})
        assert cfg.mmr_lambda == 0.5
        assert cfg.enable_cross_encoder is True

    def test_clamp_lambda(self):
        cfg = parse_mmr_config({"mmr_lambda": 2.0})
        assert cfg.mmr_lambda == 1.0


# ════════════════════════════════════════════════════════════════════════════
# MMRReranker
# ════════════════════════════════════════════════════════════════════════════
class TestMMRReranker:
    def test_empty_input(self):
        rr = MMRReranker()
        assert rr.mmr_rerank([]) == []

    def test_single_hit_passthrough(self):
        rr = MMRReranker()
        out = rr.mmr_rerank([_hit("a", 0.9)], top_k=5)
        assert len(out) == 1
        assert out[0].rank == 0

    def test_top_k_truncates(self):
        rr = MMRReranker()
        hits = [_hit(f"m{i}", 1.0 - i * 0.1) for i in range(5)]
        out = rr.mmr_rerank(hits, top_k=3)
        assert len(out) == 3

    def test_diversifies_similar_vectors(self):
        rr = MMRReranker(MMRConfig(mmr_lambda=0.5))
        hits = [
            _hit("a", 0.9, embedding=[1.0, 0.0]),
            _hit("b", 0.85, embedding=[0.99, 0.01]),  # 与 a 几乎重叠
            _hit("c", 0.5, embedding=[0.0, 1.0]),     # 正交
        ]
        embs = {h.memory_id: h.metadata["embedding"] for h in hits}
        out = rr.mmr_rerank(hits, embs, top_k=3)
        ids = [h.memory_id for h in out]
        # 第一名应该是 a(原始最高分);第二名 c(更多样)优先于 b
        assert ids[0] == "a"
        assert ids[1] == "c"
        assert ids[2] == "b"

    def test_no_embeddings_falls_back_to_relevance(self):
        rr = MMRReranker()
        hits = [_hit("a", 0.5), _hit("b", 0.9), _hit("c", 0.7)]
        out = rr.mmr_rerank(hits, embeddings={}, top_k=3)
        # 无向量 → 相似度全 0,等价按 relevance 选
        ids = [h.memory_id for h in out]
        assert ids == ["b", "c", "a"]

    def test_doesnt_mutate_input(self):
        rr = MMRReranker()
        hits = [_hit("a", 0.5), _hit("b", 0.4)]
        original_scores = [h.score for h in hits]
        rr.mmr_rerank(hits, top_k=2)
        assert [h.score for h in hits] == original_scores

    def test_rank_filled(self):
        rr = MMRReranker()
        out = rr.mmr_rerank([_hit("a", 0.5), _hit("b", 0.4)], top_k=2)
        assert [h.rank for h in out] == [0, 1]


# ════════════════════════════════════════════════════════════════════════════
# Cross-Encoder hook
# ════════════════════════════════════════════════════════════════════════════
class TestCrossEncoder:
    def test_disabled_passthrough(self):
        rr = MMRReranker(MMRConfig(enable_cross_encoder=False))
        hits = [_hit("a", 0.5), _hit("b", 0.4)]
        out = rr.cross_encode_top_k("query", hits)
        assert [h.memory_id for h in out] == ["a", "b"]

    def test_enabled_reranks(self):
        # cross_encoder 给出反向分数(b 更高)
        def ce(q: str, h: RankedMemory) -> float:
            return {"a": 0.1, "b": 0.9}.get(h.memory_id, 0.0)

        rr = MMRReranker(MMRConfig(enable_cross_encoder=True), cross_encoder=ce)
        out = rr.cross_encode_top_k("q", [_hit("a", 0.5), _hit("b", 0.4)])
        assert [h.memory_id for h in out] == ["b", "a"]


# ════════════════════════════════════════════════════════════════════════════
# 插件层
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestMMRPlugin:
    async def test_basic_rerank(self):
        plugin = MMRRerankerPlugin()
        await plugin.start({})
        hits = [_hit("a", 0.9), _hit("b", 0.85), _hit("c", 0.5)]
        out = await plugin.rerank("q", hits, top_k=2)
        assert len(out) == 2
        assert out[0].memory_id == "a"

    async def test_extracts_embeddings_from_metadata(self):
        plugin = MMRRerankerPlugin()
        await plugin.start({"mmr_lambda": 0.5})
        hits = [
            _hit("a", 0.9, embedding=[1.0, 0.0]),
            _hit("b", 0.85, embedding=[0.99, 0.01]),
            _hit("c", 0.5, embedding=[0.0, 1.0]),
        ]
        out = await plugin.rerank("q", hits, top_k=3)
        ids = [h.memory_id for h in out]
        assert ids[0] == "a"
        # 多样性:c 优先于 b
        assert ids[1] == "c"

    async def test_top_k_none_keeps_all(self):
        plugin = MMRRerankerPlugin()
        await plugin.start({})
        hits = [_hit(f"m{i}", 1.0 - i * 0.1) for i in range(4)]
        out = await plugin.rerank("q", hits, top_k=None)
        assert len(out) == 4

    async def test_health(self):
        plugin = MMRRerankerPlugin()
        await plugin.start({"mmr_lambda": 0.6})
        h = await plugin.health()
        assert h["status"] == "ok"
        assert "0.6" in h["detail"]
