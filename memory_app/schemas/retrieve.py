"""``POST /v1/memory/retrieve`` 检索契约（设计文档 §6）。

═══════════════════════════════════════════════════════════════════════════════
请求 / 响应
═══════════════════════════════════════════════════════════════════════════════
- :class:`RetrieveMemRequest`   入参（含查询、Top-K、意图、过滤、配置覆盖）
- :class:`RetrieveMemResponse`  出参（含命中 + 总数 + 调试信息）
- :class:`MemoryHit`            单条命中记录（含证据链）
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# ════════════════════════════════════════════════════════════════════════════
# 枚举与可选字段
# ════════════════════════════════════════════════════════════════════════════
class RetrievalIntent(str, Enum):
    """查询意图。

    Phase 4 引入 ``IntentClassifier`` SPI 后，``AUTO`` 由 LLM/规则自动判定；
    Phase 1 / Phase 2 阶段调用方可手动指定。
    """

    AUTO = "auto"
    FACTUAL = "factual"  # 事实问答（精确匹配优先）
    OPINION = "opinion"  # 观点/偏好（情景记忆优先）
    TEMPORAL = "temporal"  # 时序问题（时间窗 + 多跳）
    MULTI_HOP = "multi_hop"  # 多跳推理（需要图遍历或多轮检索）


class RetrievalConfig(BaseModel):
    """检索时的请求级临时覆盖（对应 §2.8 五级覆盖中的 request 层）。

    所有字段都是 Optional —— 不传即沿用配置中心当前值。
    设计文档 §2.8.2 受白名单约束：并非所有参数都允许 request 层覆盖。
    """

    model_config = ConfigDict(extra="forbid")

    over_fetch_factor: int | None = None  # 各通道过取倍数（§6.1.2，常规 4 / 复杂 12）
    similarity_threshold: float | None = None  # 阈值过滤（§6.4，默认 0.55）
    enabled_channels: list[str] | None = None  # 仅启用指定通道


# ════════════════════════════════════════════════════════════════════════════
# 请求
# ════════════════════════════════════════════════════════════════════════════
class RetrieveMemRequest(BaseModel):
    """检索请求。"""

    model_config = ConfigDict(extra="forbid")

    # ── 多租户隔离根 ──
    tenant_id: str = Field(..., min_length=1, max_length=128)
    user_id: str = Field(..., min_length=1, max_length=128)

    # ── 查询 ──
    query: str
    top_k: int = Field(default=10, ge=1, le=100)
    intent: RetrievalIntent = RetrievalIntent.AUTO

    # ── 通道开关（Phase 3+ 启用图）──
    enable_graph: bool = False

    # ── 临时覆盖 ──
    retrieval_config: RetrievalConfig | None = None
    request_override: dict | None = None  # 对接 ConfigCenter request 层

    # ── 结构化过滤 ──
    filters: dict | None = None  # 形如 {"memory_type": "EPISODIC", "time_range": [...]}

    # ── 调试 ──
    debug: bool = False  # True 时响应附带 channel scores / mmr 路径等


# ════════════════════════════════════════════════════════════════════════════
# 响应
# ════════════════════════════════════════════════════════════════════════════
class MemoryHit(BaseModel):
    """单条命中记录。"""

    model_config = ConfigDict(extra="allow")  # 允许后续 Phase 增字段

    memory_id: str
    content: str
    score: float
    memory_type: str  # 来自 :class:`memory_app.internal_models.MemoryType` 的字符串值
    source_episodes: list[str] = Field(default_factory=list)  # 证据链
    metadata: dict = Field(default_factory=dict)


class RetrieveMemResponse(BaseModel):
    """检索响应。"""

    model_config = ConfigDict(extra="allow")

    memories: list[MemoryHit] = Field(default_factory=list)
    total: int = 0
    query: str = ""

    #: 仅当请求中 ``debug=true`` 时返回，含通道分数 / MMR 路径等
    debug: dict | None = None


__all__ = [
    "RetrieveMemRequest",
    "RetrieveMemResponse",
    "MemoryHit",
    "RetrievalConfig",
    "RetrievalIntent",
]
