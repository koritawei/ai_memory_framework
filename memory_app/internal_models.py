"""内部数据模型（设计文档 §4）。

═══════════════════════════════════════════════════════════════════════════════
认知分层
═══════════════════════════════════════════════════════════════════════════════
::

    RawData (归一化通用载体)
        │ SBD 滑动窗口
        ▼
    MemCell (情景前驱单元，含 episode + semantic_memories[10] + event_log)
        │ EpisodeMemoryExtractor
        ▼
    EpisodicMemory ──┐
                     ├── ProfileMemory（用户画像，跨会话稳定特质）
                     │
                     │ ClusterManager（增量质心）
                     ▼
                 MemScene (情景簇视图)
                     │ SemanticMemoryExtractor + Consolidator
                     ▼
                 SemanticMemory (事实/偏好/目标)
                  + EventLog (原子事实 + 1024d embedding)
                  + 横向 MetaMemory (溯源/生命周期/访问控制)

═══════════════════════════════════════════════════════════════════════════════
Phase 1 简化范围
═══════════════════════════════════════════════════════════════════════════════
本模块仅实现 Vibe Coding 第 1.2 步要求的 **核心字段集**。
完整的 §4 Proto 字段（如 EpisodicMemory 的六维感官细节、ProfileMemory 的 18
维特征）将在 Phase 2-3 写入管线落地时按需扩展，**不破坏既有 API**。

═══════════════════════════════════════════════════════════════════════════════
ID 字段约定
═══════════════════════════════════════════════════════════════════════════════
所有顶层记忆体的主键字段（``mem_cell_id`` / ``episode_id`` / ``semantic_id``
等）默认值为 ``str(uuid.uuid4())``：
- 客户端可显式传入（如评测复现场景）
- 服务端不传则自动生成
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from memory_app._compat import utcnow


# ════════════════════════════════════════════════════════════════════════════
# 枚举
# ════════════════════════════════════════════════════════════════════════════
class MemoryType(str, Enum):
    """记忆三类顶层组织（设计文档 §4.3）。"""

    EPISODIC = "EPISODIC"      # 情景记忆：个人经历的具体事件
    SEMANTIC = "SEMANTIC"      # 语义记忆：去情境化的抽象知识
    PROFILE = "PROFILE"        # 用户画像：跨会话稳定特质（§4.5）
    META = "META"              # 元记忆：溯源 / 生命周期 / 访问控制


class MemoryState(str, Enum):
    """生命周期四态（设计文档 §7.1.3）。

    与 Langevin SDE 的 Poincaré 半径区间严格对应（Phase 2+ 启用 SDE 后生效）：

    ::

        ACTIVE   r < 0.3        高频活跃，球心附近
        WARM     0.3 ≤ r < 0.7  中频访问
        COLD     0.7 ≤ r < 1.0  低频，接近球面边界
        ARCHIVED 由被动衰减判定  退出 SDE 管控，进入深度存储

    Phase 1 阶段尚未启用 SDE，新记忆默认 ``ACTIVE``，状态变更由
    :class:`memory_app.plugins.spi.forgetting_policy.ForgettingPolicy` 推动。
    """

    ACTIVE = "ACTIVE"
    WARM = "WARM"
    COLD = "COLD"
    ARCHIVED = "ARCHIVED"


class ConsolidationStatus(str, Enum):
    """巩固管线状态（设计文档 §7.4）。"""

    PENDING = "pending"            # 待巩固
    NREM_PROCESSED = "nrem_processed"  # NREM 阶段处理过（事实级去重）
    REM_PROCESSED = "rem_processed"    # REM 阶段处理过（跨周主题归纳）
    DONE = "done"


# ════════════════════════════════════════════════════════════════════════════
# RawData —— 内部通用载体
# ════════════════════════════════════════════════════════════════════════════
class RawData(BaseModel):
    """归一化通用载体（设计文档 §4.2）。

    所有外部输入（ConversationTurn / 上传文档 / 事件日志）经
    :func:`memory_app.format_transfer.ingest_to_raw_data_list` 转换后，
    统一为本结构再进入 SBD。
    """

    model_config = ConfigDict(extra="allow")

    raw_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    user_id: str
    session_id: str

    raw_data_type: str = "CONVERSATION"
    content: str  # 对话文本 / 文档片段 / 事件描述
    event_time: datetime  # 原始事件发生时间，进入 SBD 时间窗判定

    #: 系统元数据：原始外部 ID、说话人、roomId 等
    metadata: dict[str, Any] = Field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
# MemCell —— 情景前驱单元
# ════════════════════════════════════════════════════════════════════════════
class MemCell(BaseModel):
    """SBD 切边界后的情景前驱单元（设计文档 §4.10）。

    定位：管线中间产物，仅做溯源 / 异常重放；不参与最终检索。
    Phase 6 起由定时清理任务在 30 天后物理删除（``consumed=true`` 标记）。
    """

    model_config = ConfigDict(extra="allow")

    # ── 标识 ──
    mem_cell_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    user_id: str
    session_id: str | None = None  # 单人会话用此
    group_id: str | None = None    # 群聊场景用此

    # ── 来源溯源 ──
    raw_data_ids: list[str] = Field(default_factory=list)
    #: 写入幂等用（设计文档 §12.1 / §5.1.3.9）：
    #: 同一 source_message_id 多次提交只触发一次 SBD
    source_message_ids: list[str] = Field(default_factory=list)

    # ── 内容 ──
    text: str  # 拼接后的对话文本块
    summary: str | None = None  # ≤200 字摘要
    subject: str | None = None  # 主题标题
    episode: str | None = None  # 第三人称完整叙述（LLM 生成）
    keywords: list[str] = Field(default_factory=list)

    # ── 向量 ──
    embedding: list[float] | None = None  # 1024 维 (Qwen3-Embedding-4B)

    # ── 参与者 ──
    participants: list[str] = Field(default_factory=list)

    # ── 生命周期（Phase 2+ 启用 SDE 后填充）──
    state: MemoryState = MemoryState.ACTIVE
    strength: float = 1.0  # 自适应强化强度，[0, S_max=5.0]
    access_count: int = 0
    importance_score: float = 0.0  # FSFM 四维综合评分（§7.2）

    # ── 时间 ──
    timestamp: datetime = Field(default_factory=utcnow)  # 事件时间
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # ── 处理状态 ──
    consolidation_status: ConsolidationStatus = ConsolidationStatus.PENDING


# ════════════════════════════════════════════════════════════════════════════
# EpisodicMemory —— 情景记忆
# ════════════════════════════════════════════════════════════════════════════
class EpisodicMemory(BaseModel):
    """情景记忆（设计文档 §4.4.1）。

    Phase 1 仅承载核心叙述维度；情绪 / 时空 / 感官 / 自我视角等其他五维
    在 Phase 3 引入 EpisodeExtractor 时再扩展。
    """

    model_config = ConfigDict(extra="allow")

    # ── 标识 ──
    episode_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    mem_cell_id: str  # 来源 MemCell 主键
    tenant_id: str
    user_id: str

    # ── 核心叙述 ──
    summary: str
    content: str | None = None
    subject: str | None = None
    episode: str | None = None  # 第三人称完整叙述

    # ── 关联实体 ──
    key_entities: list[str] = Field(default_factory=list)

    # ── 情绪（Phase 3 扩展）──
    emotional_valence: float = 0.0  # [-1.0, 1.0]，正值正向 / 负值负向 / 0 中性
    emotional_salience: float | None = None  # [0, 1]
    emotion_type: str | None = None  # joy/sadness/anger/fear/surprise/disgust

    # ── 时间 ──
    timestamp: datetime = Field(default_factory=utcnow)
    event_time: str | None = None  # 事件实际发生时间 (YYYY-MM-DD)
    event_time_range: str | None = None  # 时间范围描述

    # ── 生命周期 ──
    state: MemoryState = MemoryState.ACTIVE
    strength: float = 1.0
    access_count: int = 0
    importance_score: float = 0.0

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ════════════════════════════════════════════════════════════════════════════
# SemanticMemory —— 语义记忆
# ════════════════════════════════════════════════════════════════════════════
class KnowledgeType(str, Enum):
    """语义记忆子类型（设计文档 §4.4.2）。"""

    KNOWLEDGE = "knowledge"  # 一般知识
    FACT = "fact"            # 事实陈述
    PREFERENCE = "preference"  # 用户偏好
    GOAL = "goal"            # 用户目标


class SemanticMemory(BaseModel):
    """语义记忆（设计文档 §4.4.2）。

    去情境化的抽象知识 —— 从情景中提炼出的稳定事实 / 偏好 / 目标。
    """

    model_config = ConfigDict(extra="allow")

    # ── 标识 ──
    semantic_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    user_id: str

    # ── 内容 ──
    content: str
    knowledge_type: KnowledgeType = KnowledgeType.KNOWLEDGE

    # ── 溯源 ──
    source_episode_ids: list[str] = Field(default_factory=list)
    source_memcell_ids: list[str] = Field(default_factory=list)

    # ── 时间有效期（设计文档 §4.7 SemanticMemoryItem）──
    start_time: str | None = None  # YYYY-MM-DD
    end_time: str | None = None
    duration_days: int | None = None

    # ── 置信度与生命周期 ──
    confidence: float = 1.0
    is_valid: bool = True  # Consolidator SUPERSEDE 时标记为 false
    state: MemoryState = MemoryState.ACTIVE
    strength: float = 1.0
    access_count: int = 0
    importance_score: float = 0.0
    evidence_count: int = 1  # Consolidator UPDATE 时递增

    # ── 向量 ──
    embedding: list[float] | None = None

    # ── 时间 ──
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ════════════════════════════════════════════════════════════════════════════
# MemScene —— 情景聚类视图
# ════════════════════════════════════════════════════════════════════════════
class MemScene(BaseModel):
    """情景聚类视图（设计文档 §4.6）。

    ClusterManager 增量聚类的产物；语义沉淀的输入单元。
    """

    model_config = ConfigDict(extra="allow")

    scene_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    cluster_id: str | None = None  # 对应 ClusterState 中的簇 ID
    tenant_id: str
    user_id: str
    group_id: str | None = None

    # ── 簇内容 ──
    label: str = ""  # 业务可读标签
    scene_summary: str | None = None  # LLM 生成或质心最近邻 summary
    member_episode_ids: list[str] = Field(default_factory=list)  # 成员情景记忆 ID
    member_count: int = 0

    # ── 质心 ──
    centroid: list[float] | None = None  # 1024 维 float32

    # ── 巩固状态 ──
    pending_semantic_digest: bool = True  # True = 待离线沉淀
    consolidated_semantic_ids: list[str] = Field(default_factory=list)

    # ── 时间 ──
    created_at: datetime = Field(default_factory=utcnow)
    last_updated_at: datetime = Field(default_factory=utcnow)


# ════════════════════════════════════════════════════════════════════════════
# EventLog —— 原子事实
# ════════════════════════════════════════════════════════════════════════════
class EventLog(BaseModel):
    """结构化时间线原子事实（设计文档 §4.11）。

    每条 atomic_fact 完整独立、可单独被检索。
    """

    model_config = ConfigDict(extra="allow")

    event_log_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    user_id: str
    source_episode_id: str | None = None
    source_memcell_id: str | None = None

    #: 时间字符串，例 "March 10, 2024(Sunday) at 2:00 PM"
    time: str
    atomic_facts: list[str] = Field(default_factory=list)
    #: 与 atomic_facts 一一对应的 1024 维向量列表
    fact_embeddings: list[list[float]] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)


# ════════════════════════════════════════════════════════════════════════════
# 元记忆三子结构（设计文档 §4.4.3）
# ════════════════════════════════════════════════════════════════════════════
class ProvenanceMeta(BaseModel):
    """溯源 + 变更 + 关联（§4.4.3 子结构一）。"""

    model_config = ConfigDict(extra="allow")

    ori_event_id_list: list[str] = Field(default_factory=list)
    memcell_event_id_list: list[str] = Field(default_factory=list)
    source: str | None = None
    proposed_by: str | None = None  # 共享请求的 Agent ID

    created_at: datetime = Field(default_factory=utcnow)
    modified_at: datetime = Field(default_factory=utcnow)
    version: str = "1"

    linked_memory_ids: list[str] = Field(default_factory=list)
    linked_entities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    group_id: str | None = None
    participants: list[str] = Field(default_factory=list)


class LifecycleMeta(BaseModel):
    """生命周期 + 检索统计 + Fisher/Langevin 参数（§4.4.3 子结构二）。"""

    model_config = ConfigDict(extra="allow")

    # ── 使用频率 ──
    access_count: int = 0
    last_recalled_at: datetime | None = None
    reinforce_count: int = 0

    # ── 评分 ──
    score: float = 0.0  # 综合评分 [0, 1]
    confidence: float | None = None
    novelty: float | None = None

    # ── 生命周期状态 ──
    lifecycle: MemoryState = MemoryState.ACTIVE
    consolidation_status: ConsolidationStatus = ConsolidationStatus.PENDING

    # ── 检索统计 ──
    retrieval_count: int = 0
    retrieval_recency: float | None = None  # 距今小时数

    # ── 信息几何（Phase 2+，§7.1）──
    fisher_mean: list[float] = Field(default_factory=list)
    fisher_variance: list[float] = Field(default_factory=list)
    langevin_position: list[float] = Field(default_factory=list)  # 8 维 Poincaré


class AccessControlMeta(BaseModel):
    """访问控制（§4.4.3 子结构三）。"""

    model_config = ConfigDict(extra="allow")

    visibility_scope: str = "private"  # private / team_shared / global
    shared_agent_ids: list[str] = Field(default_factory=list)
    share_status: str | None = None  # pending / approved / rejected


class MetaMemory(BaseModel):
    """元记忆组合容器（§4.4.3）。每条 EpisodicMemory / SemanticMemory 关联一条。"""

    model_config = ConfigDict(extra="allow")

    meta_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    user_id: str

    target_memory_id: str  # 关联的目标记忆 ID（EpisodicMemory 或 SemanticMemory）
    target_memory_type: MemoryType = MemoryType.EPISODIC

    provenance: ProvenanceMeta = Field(default_factory=ProvenanceMeta)
    lifecycle: LifecycleMeta = Field(default_factory=LifecycleMeta)
    access_control: AccessControlMeta = Field(default_factory=AccessControlMeta)

    expected_response: str | None = None
    extra_json: dict | None = None


# ════════════════════════════════════════════════════════════════════════════
# 检索辅助类型
# ════════════════════════════════════════════════════════════════════════════
class RankedMemory(BaseModel):
    """检索通道返回的单条带分数记忆（设计文档 §6 各通道接口）。"""

    model_config = ConfigDict(extra="allow")

    memory_id: str
    memory_type: MemoryType = MemoryType.EPISODIC
    content: str
    score: float
    rank: int | None = None  # 通道内的排名（用于 RRF）
    source_channel: str | None = None  # 通道名（"bm25" / "vector" / "entity" / "graph"）
    metadata: dict = Field(default_factory=dict)


__all__ = [
    # 枚举
    "MemoryType",
    "MemoryState",
    "KnowledgeType",
    "ConsolidationStatus",
    # 数据模型
    "RawData",
    "MemCell",
    "EpisodicMemory",
    "SemanticMemory",
    "MemScene",
    "EventLog",
    # 元记忆
    "ProvenanceMeta",
    "LifecycleMeta",
    "AccessControlMeta",
    "MetaMemory",
    # 检索辅助
    "RankedMemory",
]
