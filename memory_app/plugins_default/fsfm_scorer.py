"""``fsfm_4d`` —— Phase 5 Step 5.3 默认重要性评分插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.importance_scorer.ImportanceScorer` 的默认实现。
内部委托 :class:`memory_app.scoring.FSFMScorer` 算法。

═══════════════════════════════════════════════════════════════════════════════
注意:SPI 三个 score_* 方法的输入差异
═══════════════════════════════════════════════════════════════════════════════
- ``score_episodic(EpisodicMemory)`` —— LLM / 规则混合
- ``score_semantic(SemanticMemory)`` —— 同上
- ``score_ref(MemoryRef)``           —— 轻量,只用 MemoryRef 字段(不调 LLM)

Phase 5 简化:三个方法都用相同算法的不同适配,均不调 LLM(LLM 评估留给 Phase 6 巩固)。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from memory_app.internal_models import (
    EpisodicMemory,
    MemCell,
    SemanticMemory,
)
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.forgetting_policy import MemoryRef
from memory_app.plugins.spi.importance_scorer import (
    ImportanceScore,
    ImportanceScorer,
)
from memory_app.scoring import FSFMScorer, parse_fsfm_config

logger = logging.getLogger(__name__)


@register
class FSFM4DScorer(ImportanceScorer):
    """FSFM 四维评分插件(Phase 5 默认)。"""

    meta = PluginMeta(
        name="fsfm_4d",
        category="memory.lifecycle.importance_scorer",
        version="1.0.0",
        description="FSFM 四维加权:CQA + BVE + TRS + SRC(归一化版本)",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "w_cqa": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.25},
                "w_bve": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.30},
                "w_trs": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.25},
                "w_src": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.20},
                "trs_half_life_days": {
                    "type": "number", "minimum": 1.0, "default": 30.0
                },
            },
        },
    )

    def __init__(self) -> None:
        self._core: FSFMScorer = FSFMScorer()

    async def start(self, config: Mapping[str, Any]) -> None:
        cfg = parse_fsfm_config(dict(config))
        self._core = FSFMScorer(config=cfg)
        logger.info(
            "fsfm_4d started: w_cqa=%.2f w_bve=%.2f w_trs=%.2f w_src=%.2f half_life=%.1fd",
            cfg.w_cqa, cfg.w_bve, cfg.w_trs, cfg.w_src, cfg.trs_half_life_days,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"weights=(cqa={self._core.config.w_cqa}, bve={self._core.config.w_bve}, "
                f"trs={self._core.config.w_trs}, src={self._core.config.w_src}), "
                f"half_life={self._core.config.trs_half_life_days}d"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # 同步算分(供 FeedbackService / RetrievalPipeline 直接消费)
    # ────────────────────────────────────────────────────────────────────────
    def score_cell(self, cell: MemCell, now: datetime | None = None) -> float:
        return self._core.score(cell, now=now)

    def detail(self, cell: MemCell, now: datetime | None = None) -> dict[str, float]:
        return self._core.detail(cell, now=now)

    # ────────────────────────────────────────────────────────────────────────
    # SPI:三个 score_* 方法
    # ────────────────────────────────────────────────────────────────────────
    async def score_episodic(self, mem: EpisodicMemory) -> ImportanceScore:
        # EpisodicMemory 没有 raw_data_ids,SRC 退化为 source_episode 计数
        cell_like = self._episodic_to_celllike(mem)
        d = self._core.detail(cell_like)
        return ImportanceScore(
            cqa=d["cqa"], bve=d["bve"], trs=d["trs"], src=d["src"],
            composite=d["composite"],
        )

    async def score_semantic(self, mem: SemanticMemory) -> ImportanceScore:
        cell_like = self._semantic_to_celllike(mem)
        d = self._core.detail(cell_like)
        return ImportanceScore(
            cqa=d["cqa"], bve=d["bve"], trs=d["trs"], src=d["src"],
            composite=d["composite"],
        )

    async def score_ref(self, mem: MemoryRef) -> ImportanceScore:
        """轻量评分;无 text 字段时 CQA / SRC 退化为 0,只看 BVE / TRS。"""
        cell_like = MemCell(
            tenant_id="-",
            user_id="-",
            text="",  # 无文本
            strength=float(mem.strength),
            access_count=int(mem.access_count),
            created_at=mem.created_at,
        )
        d = self._core.detail(cell_like)
        return ImportanceScore(
            cqa=d["cqa"], bve=d["bve"], trs=d["trs"], src=d["src"],
            composite=d["composite"],
        )

    # ────────────────────────────────────────────────────────────────────────
    # 内部:把 SPI 类型适配为算法可消费的 MemCell-like
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _episodic_to_celllike(mem: EpisodicMemory) -> MemCell:
        return MemCell(
            tenant_id=mem.tenant_id,
            user_id=mem.user_id,
            text=mem.summary or mem.content or "",
            raw_data_ids=[mem.mem_cell_id] if mem.mem_cell_id else [],
            strength=float(mem.strength),
            access_count=int(mem.access_count),
            created_at=mem.created_at,
        )

    @staticmethod
    def _semantic_to_celllike(mem: SemanticMemory) -> MemCell:
        return MemCell(
            tenant_id=mem.tenant_id,
            user_id=mem.user_id,
            text=mem.content or "",
            raw_data_ids=list(mem.source_episode_ids or []) + list(
                mem.source_memcell_ids or []
            ),
            strength=float(mem.strength),
            access_count=int(mem.access_count),
            created_at=mem.created_at,
        )


__all__ = ["FSFM4DScorer"]
