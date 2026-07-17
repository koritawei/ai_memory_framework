"""ProfileExtractor SPI —— 用户画像抽取（设计文档 §5.1.5.12）。

从 MemCell 簇中归纳跨会话稳定的用户特质（性格 / 价值观 / 工作习惯等）。
默认实现按 :class:`ScenarioType` 在 group_chat / assistant 两类场景做差异化抽取。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.internal_models import MemCell
from memory_app.plugins.base import Plugin
from .episode_extractor import ScenarioType


class ProfileExtractor(Plugin):
    """用户画像抽取扩展点。"""

    @abstractmethod
    async def extract(
        self,
        memcells: list[MemCell],
        user_id: str,
        old_profile: dict | None = None,
        scenario: ScenarioType = ScenarioType.GROUP_CHAT,
    ) -> dict:
        """从 MemCell 簇 + 历史画像增量抽取新画像。

        约定：
        - ``memcells`` 应来自同一聚类簇（由 ClusterManager 保证）
        - ``old_profile`` 非空时做增量合并（不要返回空集覆盖历史）
        - 输出形态遵循设计文档 §4.5 ProfileMemory 字段集
        - 实现应在 ValueDiscriminator 判定为高价值的簇上才被调用
        """


__all__ = ["ProfileExtractor"]
