"""IntentClassifier SPI —— 查询意图分类（设计文档 §6.0）。

默认实现 ``rule_intent_classifier``（关键词 + 时间词正则）；
Phase 2+ 可切到 ``llm_intent_classifier``（更精准但更贵）。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.plugins.base import Plugin


class IntentClassifier(Plugin):
    """意图分类扩展点。"""

    @abstractmethod
    async def classify(self, query: str) -> str:
        """返回意图字符串。

        约定：
        - 输出应为 :class:`memory_app.schemas.retrieve.RetrievalIntent` 的某个值
          （``factual / opinion / temporal / multi_hop``）；
          无法判定时返回 ``"factual"`` 作为安全兜底（最常见类型）
        - 单次调用应 < 10ms（默认规则实现）
        - LLM 实现应有缓存（query hash → intent，5 分钟 TTL）
        """


__all__ = ["IntentClassifier"]
