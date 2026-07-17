"""外部 API 契约 Pydantic 模型（设计文档 §3 输入层 + §6 检索 + §7.5 反馈）。

═══════════════════════════════════════════════════════════════════════════════
模块组织
═══════════════════════════════════════════════════════════════════════════════
- :mod:`ingest`    `POST /v1/memory/ingest` 写入契约（§3）
- :mod:`retrieve`  `POST /v1/memory/retrieve` 检索契约（§6）
- :mod:`feedback`  `POST /v1/memory/feedback` 反馈契约（§7.5）

═══════════════════════════════════════════════════════════════════════════════
设计原则
═══════════════════════════════════════════════════════════════════════════════
- **多租户隔离根**：所有请求**强制**携带 ``tenant_id`` + ``user_id``
- **评测兼容**：原生支持 LongMemEval / LoCoMo 标注字段
- **内/外解耦**：本目录是"对外契约"，内部数据模型在
  :mod:`memory_app.internal_models`，二者通过 :mod:`memory_app.format_transfer`
  转换 —— 让 API 可以灰度演进而不冲击内部实现
"""

from .feedback import FeedbackRequest, FeedbackType
from .ingest import (
    ConversationTurn,
    HistorySession,
    MemoryIngestRequest,
    RawDataType,
    RoleEnum,
)
from .retrieve import (
    MemoryHit,
    RetrievalConfig,
    RetrievalIntent,
    RetrieveMemRequest,
    RetrieveMemResponse,
)

__all__ = [
    # ingest
    "MemoryIngestRequest",
    "HistorySession",
    "ConversationTurn",
    "RoleEnum",
    "RawDataType",
    # retrieve
    "RetrieveMemRequest",
    "RetrieveMemResponse",
    "MemoryHit",
    "RetrievalConfig",
    "RetrievalIntent",
    # feedback
    "FeedbackRequest",
    "FeedbackType",
]
