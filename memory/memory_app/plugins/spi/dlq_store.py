"""DLQStore SPI —— Dead-Letter Queue。

ES / Milvus / Entity Store 等异步派生索引写入失败时，记录入 DLQ；
:class:`MemorySyncReconciler` 定时扫描重试。
"""

from __future__ import annotations

from abc import abstractmethod
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from memory_app._compat import utcnow
from memory_app.plugins.base import Plugin


class DLQRecord(BaseModel):
    """DLQ 一条记录。"""

    model_config = ConfigDict(extra="allow")

    record_id: str
    memory_id: str            # 关联的记忆 ID
    sink: Literal["es", "milvus", "entity_store", "graph"] | str
    error: str
    payload: dict             # 原始写入数据（重试用）
    retry_count: int = 0
    next_retry_at: datetime
    created_at: datetime = Field(default_factory=utcnow)


class DLQStore(Plugin):
    """DLQ 扩展点。"""

    @abstractmethod
    async def enqueue(self, record: DLQRecord) -> None:
        """入队一条失败记录。

        约定：实现应为幂等（同 ``record_id`` 重复入队不抛异常，仅 ``retry_count++``）。
        """

    @abstractmethod
    async def dequeue_due(self, limit: int = 100) -> list[DLQRecord]:
        """拉取所有 ``next_retry_at <= now`` 的记录。

        约定：实现应做"租约"机制 —— 拉取后该记录在短时间内（如 5 min）不会被
        再次拉取，避免多副本 reconciler 同时重试。
        """

    @abstractmethod
    async def mark_resolved(self, record_id: str) -> bool:
        """标记一条 DLQ 已成功重试，从队列移除。"""

    @abstractmethod
    async def mark_failed(self, record_id: str, error: str, next_retry_in_seconds: int) -> None:
        """标记一条 DLQ 重试失败，``retry_count++`` 并刷新 ``next_retry_at``。

        约定：``retry_count >= 5`` 时实现应触发告警（或转入永久失败队列）。
        """


__all__ = ["DLQStore", "DLQRecord"]
