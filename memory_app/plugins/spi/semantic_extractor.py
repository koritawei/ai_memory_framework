"""SemanticExtractor SPI —— 语义记忆联想抽取（设计文档 §5.1.3.7）。

从 :class:`EpisodicMemory` 联想出可能的稳定语义知识。
默认实现「10 联想策略」—— LLM 基于情景内容产出恰好 10 条
``SemanticMemory``（含时间有效期）。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.internal_models import EpisodicMemory, MemCell, SemanticMemory
from memory_app.plugins.base import Plugin


class SemanticExtractor(Plugin):
    """语义记忆抽取扩展点。"""

    @abstractmethod
    async def extract_for_episode(self, episode: EpisodicMemory) -> list[SemanticMemory]:
        """从单条 EpisodicMemory 联想出多条 SemanticMemory。

        约定：
        - 默认实现产出 10 条；其他实现可自定义数量但应在 [3, 20] 之间
        - 每条必须填 ``source_episode_ids = [episode.episode_id]``
        - 时间有效期字段（``start_time / end_time / duration_days``）应基于
          ``episode.event_time`` 智能计算
        - LLM 失败应包装为 :class:`PluginError(category="dependency", retryable=True)`
        """

    @abstractmethod
    async def extract_for_memcell(self, memcell: MemCell) -> list[SemanticMemory]:
        """从单个 MemCell 直接联想（SBD 阶段调用，不经过情景抽取）。

        约定：与 :meth:`extract_for_episode` 同等约束；``source_memcell_ids``
        填 ``[memcell.mem_cell_id]``，``source_episode_ids`` 留空。
        """


__all__ = ["SemanticExtractor"]
