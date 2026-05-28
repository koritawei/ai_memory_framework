"""ServiceBuilders 注册表 —— 顺序敏感的装配序列。

把各业务 builder 类挂在 :data:`BUILDERS` 列表里；
:meth:`AppState.init` 顺序遍历执行,任一失败仅 warn,不阻断后续。
"""

from __future__ import annotations

from memory_app.deps.builders.base import ServiceBuilder
from memory_app.deps.builders.cold_path import ColdPathServiceBuilder
from memory_app.deps.builders.consolidation import ConsolidationServiceBuilder
from memory_app.deps.builders.feedback import FeedbackLifecycleBuilder
from memory_app.deps.builders.graph import GraphComponentsBuilder
from memory_app.deps.builders.ingest import IngestServiceBuilder
from memory_app.deps.builders.retrieval import RetrievalOrchestratorBuilder

#: 顺序敏感：cold_path 依赖 ingest_service 已装配，feedback 依赖 retrieval，
#: graph 依赖 retrieval + cold_path。顺序与原 ``deps.py`` 内 init 调用次序一致。
BUILDERS: list[ServiceBuilder] = [
    IngestServiceBuilder(),           # 写入热路径
    ColdPathServiceBuilder(),         # 冷路径(可挂接到 IngestService)
    RetrievalOrchestratorBuilder(),   # 检索
    FeedbackLifecycleBuilder(),       # 反馈与生命周期(可挂接到 RetrievalOrchestrator)
    ConsolidationServiceBuilder(),    # 离线巩固
    GraphComponentsBuilder(),         # 图与实体(可挂接到 RetrievalOrchestrator + ColdPath)
]


__all__ = [
    "BUILDERS",
    "ServiceBuilder",
    "IngestServiceBuilder",
    "ColdPathServiceBuilder",
    "RetrievalOrchestratorBuilder",
    "FeedbackLifecycleBuilder",
    "ConsolidationServiceBuilder",
    "GraphComponentsBuilder",
]
