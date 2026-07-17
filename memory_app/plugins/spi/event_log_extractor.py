"""EventLogExtractor SPI —— 事件日志原子事实抽取（设计文档 §5.1.3.8）。

把情景叙述拆解为结构化、独立可检索的「原子事实」，每条原子事实
带独立时间戳 + 1024 维向量嵌入。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.internal_models import EventLog
from memory_app.plugins.base import Plugin


class EventLogExtractor(Plugin):
    """事件日志抽取扩展点。"""

    @abstractmethod
    async def extract(self, episode_text: str, time_str: str) -> EventLog:
        """从情景文本 + 时间字符串抽取原子事实列表。

        约定：
        - 时间字符串格式如 ``"March 10, 2024(Sunday) at 2:00 PM"``，便于 LLM 解析
        - 返回的 ``EventLog.atomic_facts`` 长度应与 ``fact_embeddings`` 一致
        - 每条 atomic_fact 必须**完整独立**，可单独被检索而不丢失语义
        - 抽取失败（LLM JSON 解析失败 4 次以上）抛 :class:`PluginError`
        """


__all__ = ["EventLogExtractor"]
