"""ImportanceScorer SPI —— FSFM 四维重要性评分（设计文档 §7.2）。

默认实现 ``fsfm_4d``：CQA + BVE + TRS + SRC 加权综合，[-1.5, 2.25]。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel, Field

from memory_app.internal_models import EpisodicMemory, SemanticMemory
from memory_app.plugins.base import Plugin
from .forgetting_policy import MemoryRef


class ImportanceScore(BaseModel):
    """四维评分结果。"""

    cqa: float = Field(default=0.0, description="内容质量 [0, 3]")
    bve: float = Field(default=0.0, description="业务价值 [0, 3]")
    trs: float = Field(default=0.0, description="时间相关性 [0, 2]")
    src: float = Field(default=0.0, description="安全风险 [-10, 0]")
    composite: float = Field(default=0.0, description="加权综合 [-1.5, 2.25]")


class ImportanceScorer(Plugin):
    """重要性评分扩展点。"""

    @abstractmethod
    async def score_episodic(self, mem: EpisodicMemory) -> ImportanceScore:
        """对单条 EpisodicMemory 评分。

        约定：
        - CQA 由 LLM 评估或基于规则（长度 / 实体数 / 信息密度）
        - BVE 由分类映射（用户偏好 / 历史决策 / 临时状态）
        - TRS = 2 × exp(-λΔt) × min(1, access_count/10) × context_align
        - SRC 由安全扫描器 / PII 检测器评估
        - composite = 0.35×CQA + 0.25×BVE + 0.25×TRS + 0.15×SRC
        """

    @abstractmethod
    async def score_semantic(self, mem: SemanticMemory) -> ImportanceScore:
        """对单条 SemanticMemory 评分。约定同 :meth:`score_episodic`。"""

    @abstractmethod
    async def score_ref(self, mem: MemoryRef) -> ImportanceScore:
        """轻量评分（只用 MemoryRef 已有字段，不调 LLM）。

        用于离线巩固任务的全量批量评分（百万级记忆，不可能全部走 LLM）。
        """


__all__ = ["ImportanceScorer", "ImportanceScore"]
