"""Consolidator —— 语义事实冲突消解(设计文档 §5.1.6.1 / §7.4)。

═══════════════════════════════════════════════════════════════════════════════
四操作决策
═══════════════════════════════════════════════════════════════════════════════
::

    sim < update_threshold        → ADD       (新事实,直接存)
    update_threshold ≤ sim < supersede_threshold
                                 → UPDATE     (信息互补,合并)
    supersede_threshold ≤ sim < noop_threshold
                                 → SUPERSEDE  (替代旧事实)
    sim ≥ noop_threshold          → NOOP      (完全重复,跳过)

═══════════════════════════════════════════════════════════════════════════════
综合相似度(SPI 契约)
═══════════════════════════════════════════════════════════════════════════════
::

    composite_sim = w_jaccard × Jaccard(entities) + w_cos × Cosine(embedding)

- Phase 1 简化:embedding 不存时退化到 ``Jaccard(chars(content))``
- 实体集合不存时退化到 ``Jaccard(set(content_a) , set(content_b))``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from memory_app.internal_models import SemanticMemory
from memory_app.plugins.spi.consolidator import (
    ConsolidationDecision,
    ConsolidatorResult,
)
from memory_app.retrieval.reranker import cosine_similarity

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ConsolidateConfig:
    """Consolidator 的相似度阈值与权重。

    SPI 契约::

        sim < 0.85 → ADD;[0.85, 0.95) → UPDATE/SUPERSEDE;≥ 0.95 → NOOP

    本工程进一步细分 [supersede_threshold, noop_threshold] 段为 SUPERSEDE,
    便于"几乎重复但仍有微小差别"的事实做替换(标记旧 ``is_valid=false``)。
    """

    update_threshold: float = 0.85
    supersede_threshold: float = 0.93
    noop_threshold: float = 0.97

    #: Jaccard / Cosine 加权(SPI 契约 0.4 / 0.6;无 embedding 时退化到 1.0 / 0.0)
    w_jaccard: float = 0.4
    w_cosine: float = 0.6


def parse_consolidate_config(params: dict[str, Any] | None) -> ConsolidateConfig:
    cfg = ConsolidateConfig()
    if not params:
        return cfg
    for k in (
        "update_threshold",
        "supersede_threshold",
        "noop_threshold",
        "w_jaccard",
        "w_cosine",
    ):
        if k in params:
            try:
                setattr(cfg, k, float(params[k]))
            except (TypeError, ValueError):
                continue
    return cfg


# ════════════════════════════════════════════════════════════════════════════
# 相似度工具
# ════════════════════════════════════════════════════════════════════════════
def jaccard(a: Iterable, b: Iterable) -> float:
    """集合 Jaccard 相似度 |A ∩ B| / |A ∪ B|;空并集返回 0.0。"""
    set_a, set_b = set(a or []), set(b or [])
    if not set_a and not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


# ════════════════════════════════════════════════════════════════════════════
# 核心
# ════════════════════════════════════════════════════════════════════════════
class Consolidator:
    """纯算法 Consolidator(可独立单测,不依赖 SPI 装配)。

    构造参数:
        ``config``           :class:`ConsolidateConfig`
        ``embedding_client`` 任意鸭子类型;``await embed(list[str]) -> list[list[float]]``
                             None 时只用 Jaccard(content chars)
    """

    def __init__(
        self,
        config: ConsolidateConfig | None = None,
        embedding_client: Any | None = None,
    ) -> None:
        self.config = config or ConsolidateConfig()
        self.embedding_client = embedding_client

    # ────────────────────────────────────────────────────────────────────────
    # Public(纯算法版本,与 SPI consolidate 同形)
    # ────────────────────────────────────────────────────────────────────────
    async def consolidate(
        self,
        new_fact: SemanticMemory,
        existing_facts: list[SemanticMemory],
    ) -> ConsolidatorResult:
        if not existing_facts:
            return ConsolidatorResult(
                decision=ConsolidationDecision.ADD,
                target_id=None,
                composite_sim=0.0,
                reasoning="empty_existing",
            )

        best_sim = -1.0
        best_target: str | None = None
        best_jaccard = 0.0
        best_cosine = 0.0

        # 计算 new_fact 的 embedding 一次,复用
        new_emb = await self._embedding(new_fact)

        # 批量预取所有缺 embedding 的旧事实 ——
        # 旧实现是 for mem in N: await embed(mem.content) 串行 N 次 LLM 调用,
        # N=50 时 = 50 × LLM RTT。改为 1 次 batch embed,N 维度的延迟摊销为 O(1) RPC。
        old_emb_map = await self._prefetch_old_embeddings(existing_facts, new_emb is not None)

        # new_fact 的 token 集合在 N 次循环里恒定,提到循环外避免 N 次重复构造
        # (字符级 token 化对 200 字内容是 N×200 次 list-append)。
        new_tokens = self._tokens(new_fact)

        for mem in existing_facts:
            j = jaccard(new_tokens, self._tokens(mem))
            c = 0.0
            if new_emb is not None:
                old_emb = mem.embedding or old_emb_map.get(mem.semantic_id)
                if old_emb and new_emb:
                    c = cosine_similarity(list(new_emb), list(old_emb))
            sim = self._composite_sim(jaccard_v=j, cosine_v=c, has_embedding=bool(new_emb))
            if sim > best_sim:
                best_sim = sim
                best_target = mem.semantic_id
                best_jaccard = j
                best_cosine = c

        decision, reason = self._decide(best_sim)
        # 没有相似度就退化为 ADD
        if best_sim < 0:
            decision = ConsolidationDecision.ADD
            best_sim = 0.0
            best_target = None
        if decision == ConsolidationDecision.ADD:
            best_target = None
        logger.debug(
            "consolidate: sim=%.3f j=%.3f c=%.3f → %s (target=%s)",
            best_sim, best_jaccard, best_cosine, decision.value, best_target,
        )
        return ConsolidatorResult(
            decision=decision,
            target_id=best_target,
            composite_sim=round(best_sim, 6),
            reasoning=reason,
        )

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    def _decide(self, sim: float) -> tuple[ConsolidationDecision, str]:
        if sim >= self.config.noop_threshold:
            return ConsolidationDecision.NOOP, f"noop:sim={sim:.3f}>=noop_threshold"
        if sim >= self.config.supersede_threshold:
            return ConsolidationDecision.SUPERSEDE, f"supersede:sim={sim:.3f}"
        if sim >= self.config.update_threshold:
            return ConsolidationDecision.UPDATE, f"update:sim={sim:.3f}"
        return ConsolidationDecision.ADD, f"add:sim={sim:.3f}<update_threshold"

    def _composite_sim(
        self,
        *,
        jaccard_v: float,
        cosine_v: float,
        has_embedding: bool,
    ) -> float:
        """加权综合;无 embedding 时退化为 100% Jaccard。"""
        if not has_embedding:
            return jaccard_v
        wj = self.config.w_jaccard
        wc = self.config.w_cosine
        denom = wj + wc
        if denom <= 0:
            return jaccard_v
        return (wj * jaccard_v + wc * cosine_v) / denom

    @staticmethod
    def _tokens(mem: SemanticMemory) -> list[str]:
        """优先用 entities;退化到 content 字符级 token。

        SemanticMemory 没有显式 ``entities`` 字段;Phase 6 简化:把 content
        的每个非空白字符视作 token。汉字 + ASCII 都按字符切。
        """
        text = (mem.content or "").strip()
        return [ch for ch in text if not ch.isspace()]

    async def _embedding(self, mem: SemanticMemory) -> list[float] | None:
        if mem.embedding:
            return list(mem.embedding)
        if self.embedding_client is None:
            return None
        return await self._embed_text(mem.content or "")

    async def _embed_text(self, text: str) -> list[float] | None:
        if not text or self.embedding_client is None:
            return None
        try:
            out = await self.embedding_client.embed([text])
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidator embed failed: %s", e)
            return None
        if not out or not out[0]:
            return None
        return list(out[0])

    async def _prefetch_old_embeddings(
        self,
        existing: list[SemanticMemory],
        need_embeddings: bool,
    ) -> dict[str, list[float]]:
        """一次 batch 调用补齐所有缺 embedding 的旧事实。

        - 若 ``need_embeddings=False`` 或无 embedding_client,直接返回空(走 Jaccard 路径)
        - 已带 embedding 的事实不再请求
        - 返回 ``{semantic_id: embedding}``;请求失败留空,后续逐条退化到 0 cosine
        """
        if not need_embeddings or self.embedding_client is None:
            return {}
        missing: list[tuple[str, str]] = [
            (m.semantic_id, m.content or "")
            for m in existing
            if not m.embedding and (m.content or "")
        ]
        if not missing:
            return {}
        ids = [mid for mid, _ in missing]
        texts = [txt for _, txt in missing]
        try:
            vectors = await self.embedding_client.embed(texts)
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidator batch embed failed: %s", e)
            return {}
        out: dict[str, list[float]] = {}
        for mid, vec in zip(ids, vectors or []):
            if vec:
                out[mid] = list(vec)
        return out


__all__ = [
    "ConsolidateConfig",
    "parse_consolidate_config",
    "jaccard",
    "Consolidator",
]
