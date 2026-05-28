"""ConsolidationStrategy SPI —— 离线巩固策略。

默认实现 ``three_phase_dreaming``：light（6h）+ deep（每日 03:00）+ rem（每周日 05:00）。
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from memory_app.plugins.base import Plugin


class ConsolidationReport(BaseModel):
    """单次巩固任务的报告（供监控 / 审计）。"""

    model_config = ConfigDict(extra="allow")

    phase: Literal["light", "deep", "rem"]
    started_at: datetime
    finished_at: datetime
    scanned_count: int = 0           # 扫描的记忆条数
    consolidated_count: int = 0       # 实际产生 SemanticMemory 的数量
    forgotten_count: int = 0          # 进入遗忘候选的数量
    archived_count: int = 0           # 状态降级到 ARCHIVED 的数量
    error_count: int = 0
    notes: list[str] = Field(default_factory=list)


class ConsolidationStrategy(Plugin):
    """离线巩固策略扩展点。"""

    @abstractmethod
    async def run(
        self,
        scope: Literal["light", "deep", "rem", "all"] = "all",
        time: datetime | None = None,
    ) -> ConsolidationReport:
        """触发一次巩固。

        约定：
        - ``scope="light"``  仅做去重 + 弱合并（近 2 天，dedupe_sim=0.9）
        - ``scope="deep"``   做高价值记忆深度摘要 + 衰减判定
        - ``scope="rem"``    跨周扫描"模式 / 主题"，输出 CommunityNode
        - ``scope="all"``    按当前时间对应阶段自动选择
        - ``time=None``      用当前服务器时间
        - 实现必须做**分布式锁** —— 同 ``scope`` 不可并发执行
        """


__all__ = ["ConsolidationStrategy", "ConsolidationReport"]
