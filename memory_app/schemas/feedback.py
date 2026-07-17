"""``POST /v1/memory/feedback`` 反馈契约（设计文档 §7.5）。

═══════════════════════════════════════════════════════════════════════════════
反馈类型与 signal_value 对照（设计文档 §7.5）
═══════════════════════════════════════════════════════════════════════════════
| feedback_type      | signal_value | 效果           |
| ------------------ | ------------ | -------------- |
| EXPLICIT_CONFIRM   | +1.0         | 强强化         |
| POSITIVE           | +0.3         | 弱强化         |
| NEGATIVE           | -0.5         | 弱衰减         |
| CORRECTION         | -2.0         | 强衰减         |
| DELETION_REQUEST   | -10.0        | 触发主动删除（P2）|

调用方传入的 ``signal_value`` 若为 0 时，由服务端按上表填充默认值。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from memory_app._compat import utcnow


class FeedbackType(str, Enum):
    """反馈大类。"""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    CORRECTION = "correction"
    DELETION_REQUEST = "deletion_request"
    EXPLICIT_CONFIRM = "explicit_confirm"


class FeedbackRequest(BaseModel):
    """单次反馈请求。

    ``mem_cell_id`` / ``memory_id`` 二者**至少一个**必须提供：
    - ``mem_cell_id`` 指向写入侧产物（MemCell）
    - ``memory_id``   指向检索侧产物（EpisodicMemory / SemanticMemory）

    Phase 5 落地时由 :class:`memory_app.plugins.spi.reinforcer.Reinforcer`
    SPI 实际处理。
    """

    model_config = ConfigDict(extra="forbid")

    # ── 多租户隔离根 ──
    tenant_id: str
    user_id: str

    # ── 目标记忆（二选一）──
    mem_cell_id: str | None = None
    memory_id: str | None = None

    # ── 反馈本身 ──
    feedback_type: FeedbackType
    signal_value: float = 0.0  # 0 表示按 feedback_type 默认值填充

    # ── 上下文 ──
    comment: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)

    # ── 关联检索 ──
    #: 如果反馈是基于一次检索结果，可关联检索 ID 便于审计
    retrieval_id: str | None = None


__all__ = ["FeedbackRequest", "FeedbackType"]
