"""``POST /v1/memory/ingest`` 写入契约。

═══════════════════════════════════════════════════════════════════════════════
三级嵌套：MemoryIngestRequest → HistorySession → ConversationTurn
═══════════════════════════════════════════════════════════════════════════════
- **MemoryIngestRequest**  一次写入请求；多租户隔离根；含若干 history_sessions
- **HistorySession**       单个历史会话；含若干 turns；可选预生成摘要
- **ConversationTurn**     单轮对话；role + content 为最小集合

═══════════════════════════════════════════════════════════════════════════════
评测兼容
═══════════════════════════════════════════════════════════════════════════════
- LongMemEval：原生兼容 ``haystack_sessions`` + ``has_answer`` 标注
- LoCoMo：原生兼容 ``session_N`` + ``dia_id`` + ``speaker_a/b`` + 多媒体字段
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from memory_app._compat import utcnow


# ════════════════════════════════════════════════════════════════════════════
# 枚举
# ════════════════════════════════════════════════════════════════════════════
class RoleEnum(str, Enum):
    """对话角色（继承 LongMemEval 标准）。"""

    user = "user"
    assistant = "assistant"
    system = "system"


class RawDataType(str, Enum):
    """输入数据大类。

    当前版本仅承载 ``CONVERSATION``；后续支持 ``DOCUMENT`` / ``EVENT``。
    """

    CONVERSATION = "CONVERSATION"
    DOCUMENT = "DOCUMENT"
    EVENT = "EVENT"


# ════════════════════════════════════════════════════════════════════════════
# 三级嵌套：Turn → Session → Request
# ════════════════════════════════════════════════════════════════════════════
class ConversationTurn(BaseModel):
    """单轮对话。

    最小必填集：``role`` + ``content``。其他字段为 LongMemEval / LoCoMo
    评测兼容字段，生产 API 调用方可全部省略。
    """

    model_config = ConfigDict(extra="forbid")

    # ── 必填 ──
    role: RoleEnum
    content: str

    # ── 可选基础字段 ──
    turn_index: int | None = None  # 该 session 内的轮次序号（0-based）
    timestamp: datetime | None = None
    token_count: int | None = None  # 预估 token 数，便于上游做窗口控制

    # ── LoCoMo 兼容 ──
    dia_id: str | None = None  # 对话唯一 ID（如 D1:3）
    speaker_name: str | None = None  # 实际说话人名称（覆盖 role，多 Agent 场景）

    # ── LongMemEval 评测标注 ──
    has_answer: bool = False  # 该轮是否包含答案证据

    # ── 多媒体（LoCoMo）──
    img_url: str | None = None
    caption: str | None = None  # 图片 BLIP caption

    # ── 业务扩展 ──
    extra: dict | None = None


class HistorySession(BaseModel):
    """单个历史会话。

    LoCoMo / LongMemEval 评测的"haystack session"或"session_N"。
    """

    model_config = ConfigDict(extra="forbid")

    # ── 必填 ──
    session_id: str
    turns: list[ConversationTurn] = Field(default_factory=list)

    # ── 可选基础字段 ──
    session_date: datetime | None = None  # 会话发生时间（ISO8601），SBD 时间窗用
    session_start: datetime | None = None  # 兼容字段：与 session_date 同义
    session_end: datetime | None = None

    # ── 多说话人（LoCoMo）──
    speaker_a: str | None = None
    speaker_b: str | None = None

    # ── 预生成摘要（LoCoMo）──
    session_summary: str | None = None
    session_observation: str | None = None  # 第三方视角观察

    # ── 业务扩展 ──
    extra: dict | None = None


class MemoryIngestRequest(BaseModel):
    """写入请求。

    多租户隔离的根：``tenant_id`` + ``user_id`` 二者**必填**，缺一即
    ``ValidationError``。这是 多租户隔离策略的强制契约。
    """

    model_config = ConfigDict(extra="forbid")

    # ── 多租户隔离根（必填）──
    tenant_id: str
    user_id: str

    # ── 业务标识（可选）──
    session_id: str | None = None  # 当前 ingest 调用关联的会话 ID
    agent_id: str | None = None
    agent_role: str | None = None  # navigator / assistant / support 等
    adiu: str | None = None  # 业务复合标识（App-Device-Interface-User）

    # ── 时间戳 ──
    #: 服务端记录的事件时间。客户端不传则用当前 UTC，便于审计 / SBD 时间窗
    event_time: datetime = Field(default_factory=utcnow)

    # ── 类型 ──
    raw_data_type: RawDataType = RawDataType.CONVERSATION

    # ── 内容（核心三级嵌套）──
    history_sessions: list[HistorySession] = Field(default_factory=list)

    # ── 元信息扩展 ──
    metadata: dict | None = None
    extra: dict | None = None

    # ──  准入门相关字段（写入热路径 写入热路径启用）──
    #: 客户端去重键；服务端用 Redis SETNX 24h 防止重复写入
    idempotency_key: str | None = None

    #: 写入一致性偏好：strong=同步 flush 等待可被检索；eventual=fire-and-forget
    min_read_consistency: str | None = None  # "strong" | "eventual"


__all__ = [
    "MemoryIngestRequest",
    "HistorySession",
    "ConversationTurn",
    "RoleEnum",
    "RawDataType",
]
