"""``mmr`` —— 检索 MMR 重排插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.reranker.Reranker` 的默认实现。委托
:class:`memory_app.retrieval.reranker.MMRReranker` 核心算法。

═══════════════════════════════════════════════════════════════════════════════
embedding 来源
═══════════════════════════════════════════════════════════════════════════════
SPI ``rerank(query, candidates, top_k)`` 不接收 embedding 字典,因此本插件:
- 优先从 ``candidate.metadata["embedding"]`` 读
- 退而求其次:看 RankedMemory 是否在 metadata 中带 ``vector`` 字段
- 都没有 → MMR 降级为按 relevance 排(BaseReranker 已保证不崩)
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.internal_models import RankedMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.reranker import Reranker
from memory_app.retrieval.reranker import MMRReranker, parse_mmr_config

logger = logging.getLogger(__name__)


@register
class MMRRerankerPlugin(Reranker):
    """MMR 重排(检索 默认)。"""

    meta = PluginMeta(
        name="mmr",
        category="memory.retrieval.reranker",
        version="1.0.0",
        description="Maximal Marginal Relevance(λ=0.7);Cross-Encoder hook 默认关闭",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "mmr_lambda": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.7
                },
                "enable_cross_encoder": {"type": "boolean", "default": False},
            },
        },
    )

    def __init__(self) -> None:
        self._core: MMRReranker = MMRReranker()

    async def start(self, config: Mapping[str, Any]) -> None:
        cfg = parse_mmr_config(dict(config))
        self._core = MMRReranker(config=cfg)
        logger.info(
            "mmr started: lambda=%.2f, cross_encoder=%s",
            cfg.mmr_lambda, cfg.enable_cross_encoder,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"lambda={self._core.config.mmr_lambda}, "
                f"cross_encoder={self._core.config.enable_cross_encoder}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    async def rerank(
        self,
        query: str,
        candidates: list[RankedMemory],
        top_k: int | None = None,
    ) -> list[RankedMemory]:
        embeddings = self._extract_embeddings(candidates)
        out = self._core.mmr_rerank(candidates, embeddings, top_k=top_k)
        # cross-encoder hook(默认关闭)
        return self._core.cross_encode_top_k(query, out)

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _extract_embeddings(
        candidates: list[RankedMemory],
    ) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for c in candidates:
            md = c.metadata or {}
            vec = md.get("embedding") or md.get("vector")
            if vec:
                try:
                    out[c.memory_id] = [float(x) for x in vec]
                except (TypeError, ValueError):
                    continue
        return out


__all__ = ["MMRRerankerPlugin"]
