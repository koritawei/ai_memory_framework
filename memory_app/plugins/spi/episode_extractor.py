"""EpisodeExtractor SPI —— 情景记忆抽取。

把 :class:`MemCell` 提炼为 :class:`EpisodicMemory`，含核心叙述 / 情绪 /
时空 / 感官 / 自我视角等维度（当前版本仅核心叙述维度）。
"""

from __future__ import annotations

from abc import abstractmethod
from enum import Enum

from memory_app.internal_models import EpisodicMemory, MemCell, SemanticMemory
from memory_app.plugins.base import Plugin


class ScenarioType(str, Enum):
    """情景抽取场景。

    用以让 LLM prompt 在工作群聊 / 个人助手两类场景下做差异化抽取
    （冷路径 真实落地时使用）。
    """

    GROUP_CHAT = "group_chat"
    ASSISTANT = "assistant"


class EpisodeExtractor(Plugin):
    """情景记忆抽取扩展点。"""

    @abstractmethod
    async def extract(
        self,
        memcell: MemCell,
        old_memories: list[SemanticMemory] | None = None,
        scenario: ScenarioType = ScenarioType.GROUP_CHAT,
    ) -> list[EpisodicMemory]:
        """从单个 MemCell 抽取一组 EpisodicMemory（每参与者一条）。

        约定：
        - ``memcell.text`` 为空 → 返回空列表，不抛异常
        - ``memcell.participants`` 为空 → 至少返回 1 条「无参与者」记录
        - 应填充 ``EpisodicMemory.mem_cell_id = memcell.mem_cell_id`` 实现溯源
        - LLM 调用失败由实现决定是否重试；连续 5 次失败应抛
          :class:`PluginError(category="dependency", retryable=True)`
        """


__all__ = ["EpisodeExtractor", "ScenarioType"]
