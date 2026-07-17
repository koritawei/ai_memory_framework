"""Consolidator 核心 + composite 插件测试(Step 6.1)。"""

from __future__ import annotations

import pytest

from memory_app.consolidator import (
    ConsolidateConfig,
    Consolidator as CoreConsolidator,
    jaccard,
    parse_consolidate_config,
)
from memory_app.internal_models import KnowledgeType, SemanticMemory
from memory_app.plugins.spi.consolidator import ConsolidationDecision
from memory_app.plugins_default.composite_consolidator import (
    CompositeConsolidator,
)


def _mem(content: str, *, embedding=None, semantic_id: str | None = None) -> SemanticMemory:
    kw = dict(
        tenant_id="t1",
        user_id="u1",
        content=content,
        knowledge_type=KnowledgeType.FACT,
    )
    if embedding is not None:
        kw["embedding"] = embedding
    if semantic_id is not None:
        kw["semantic_id"] = semantic_id
    return SemanticMemory(**kw)


# ════════════════════════════════════════════════════════════════════════════
# 配置 / 工具
# ════════════════════════════════════════════════════════════════════════════
class TestConsolidateConfig:
    def test_default_thresholds(self):
        cfg = ConsolidateConfig()
        assert cfg.update_threshold == 0.85
        assert cfg.supersede_threshold == 0.93
        assert cfg.noop_threshold == 0.97

    def test_parse(self):
        cfg = parse_consolidate_config({"noop_threshold": 0.99})
        assert cfg.noop_threshold == 0.99


class TestJaccard:
    def test_identical(self):
        assert jaccard(["a", "b"], ["a", "b"]) == 1.0

    def test_disjoint(self):
        assert jaccard(["a", "b"], ["c", "d"]) == 0.0

    def test_partial(self):
        assert jaccard(["a", "b"], ["a", "c"]) == pytest.approx(1 / 3)

    def test_empty(self):
        assert jaccard([], []) == 0.0


# ════════════════════════════════════════════════════════════════════════════
# Consolidator 核心(无 embedding,纯 Jaccard 字符)
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestConsolidatorCore:
    async def test_add_when_empty(self):
        c = CoreConsolidator()
        out = await c.consolidate(_mem("新事实"), [])
        assert out.decision == ConsolidationDecision.ADD
        assert out.target_id is None

    async def test_noop_exact_duplicate(self):
        c = CoreConsolidator()
        existing = [_mem("用户喜欢咖啡", semantic_id="old1")]
        out = await c.consolidate(_mem("用户喜欢咖啡"), existing)
        # 完全相同字符 → Jaccard=1.0 → noop
        assert out.decision == ConsolidationDecision.NOOP
        assert out.target_id == "old1"

    async def test_add_when_unrelated(self):
        c = CoreConsolidator()
        existing = [_mem("ABCDE", semantic_id="old1")]
        out = await c.consolidate(_mem("vwxyz"), existing)
        assert out.decision == ConsolidationDecision.ADD

    async def test_picks_best_target_when_decision_not_add(self):
        """命中 UPDATE / SUPERSEDE / NOOP 时,target 必须指向最相似的候选。"""
        c = CoreConsolidator()
        # old2 与 new 完全相同 → NOOP target=old2(超过 noop_threshold)
        existing = [
            _mem("完全无关内容", semantic_id="old1"),
            _mem("用户喜欢咖啡", semantic_id="old2"),
        ]
        out = await c.consolidate(_mem("用户喜欢咖啡"), existing)
        assert out.decision == ConsolidationDecision.NOOP
        assert out.target_id == "old2"

    async def test_decision_has_reasoning(self):
        c = CoreConsolidator()
        out = await c.consolidate(_mem("x"), [_mem("x", semantic_id="o")])
        assert out.reasoning  # 非空


# ════════════════════════════════════════════════════════════════════════════
# 带 embedding 的相似度
# ════════════════════════════════════════════════════════════════════════════
class _FakeEmbedding:
    """根据文本返回固定向量的 mock。"""

    def __init__(self, mapping):
        self.mapping = mapping

    async def embed(self, texts):
        out = []
        for t in texts:
            out.append(self.mapping.get(t, [0.0]))
        return out


@pytest.mark.asyncio
class TestConsolidatorWithEmbedding:
    async def test_high_cosine_triggers_supersede(self):
        # 真正落进 SUPERSEDE 区间 [0.93, 0.97) 需要 jaccard 与 cosine 都很高。
        # 文本完全相同 → jaccard=1.0;向量微差 → cosine≈0.998。
        # composite = 0.4*1.0 + 0.6*0.998 ≈ 0.9988 → NOOP(≥0.97)。
        # 想真正命中 SUPERSEDE [0.93, 0.97),需要 jaccard~1, cosine~0.92:
        #   0.4*1 + 0.6*0.917 ≈ 0.95 ∈ [0.93, 0.97)
        # 用两条共字符相同(jaccard=1)但向量略偏离的对照。
        emb = _FakeEmbedding({
            "用户喜欢咖啡早上": [1.0, 0.0],
            "用户喜欢咖啡早上 ": [0.917, 0.4],  # cosine ≈ 0.917
        })
        c = CoreConsolidator(embedding_client=emb)
        existing = [_mem("用户喜欢咖啡早上 ", semantic_id="o1")]
        out = await c.consolidate(_mem("用户喜欢咖啡早上"), existing)
        # 现在严格断言落在 SUPERSEDE 区间;否则测试名与实际行为不符
        assert out.decision == ConsolidationDecision.SUPERSEDE, (
            f"expected SUPERSEDE, got {out.decision} (sim={out.composite_sim:.3f})"
        )
        assert 0.93 <= out.composite_sim < 0.97

    async def test_low_cosine_falls_to_add(self):
        emb = _FakeEmbedding({
            "完全不同X": [1.0, 0.0],
            "完全不同Y": [-1.0, 0.0],  # 反向
        })
        c = CoreConsolidator(embedding_client=emb)
        existing = [_mem("完全不同Y", semantic_id="o1")]
        out = await c.consolidate(_mem("完全不同X"), existing)
        assert out.decision == ConsolidationDecision.ADD

    async def test_embedding_failure_falls_back_to_jaccard(self):
        class FailingEmb:
            async def embed(self, texts):
                raise RuntimeError("embed down")

        c = CoreConsolidator(embedding_client=FailingEmb())
        existing = [_mem("ABCDE", semantic_id="o1")]
        out = await c.consolidate(_mem("ABCDE"), existing)
        # 仍能给出 NOOP / SUPERSEDE / UPDATE 决策(纯 Jaccard 100%)
        assert out.decision == ConsolidationDecision.NOOP


# ════════════════════════════════════════════════════════════════════════════
# composite 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestCompositeConsolidatorPlugin:
    async def test_decide_via_plugin(self):
        plugin = CompositeConsolidator()
        await plugin.start({})
        out = await plugin.consolidate(
            _mem("hello"), [_mem("hello", semantic_id="o1")]
        )
        assert out.decision == ConsolidationDecision.NOOP

    async def test_threshold_overrides(self):
        plugin = CompositeConsolidator()
        # 收紧 noop 阈值(>=1.01),完全相同的也不算 NOOP
        await plugin.start({"noop_threshold": 1.01, "supersede_threshold": 0.99})
        out = await plugin.consolidate(
            _mem("hello"), [_mem("hello", semantic_id="o1")]
        )
        # Jaccard=1.0 → SUPERSEDE 阈值 0.99 → 命中
        assert out.decision == ConsolidationDecision.SUPERSEDE

    async def test_health(self):
        plugin = CompositeConsolidator()
        await plugin.start({"enable_sheaf": True})
        h = await plugin.health()
        assert h["status"] == "ok"
        assert "sheaf=True" in h["detail"]

    async def test_bind_embedding(self):
        plugin = CompositeConsolidator()
        await plugin.start({})
        emb = _FakeEmbedding({"a": [1.0], "b": [0.0]})
        plugin.bind_embedding_client(emb)
        # 装配成功(不抛)
        out = await plugin.consolidate(_mem("a"), [_mem("a", semantic_id="o")])
        assert out.decision == ConsolidationDecision.NOOP
