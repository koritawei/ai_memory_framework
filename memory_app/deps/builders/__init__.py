"""ServiceBuilders 注册表 —— Phase 2 → 7 顺序敏感的装配序列。

把每个 Phase 的 builder 类挂在 :data:`BUILDERS` 列表里;
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

#: Phase 顺序敏感:cold_path 依赖 ingest_service 已装配,feedback 依赖 retrieval,
#: graph 依赖 retrieval + cold_path。顺序与原 ``deps.py`` 内 init 调用次序一致。
BUILDERS: list[ServiceBuilder] = [
    IngestServiceBuilder(),           # Phase 2
    ColdPathServiceBuilder(),         # Phase 3(可挂接到 IngestService)
    RetrievalOrchestratorBuilder(),   # Phase 4
    FeedbackLifecycleBuilder(),       # Phase 5(可挂接到 RetrievalOrchestrator)
    ConsolidationServiceBuilder(),    # Phase 6
    GraphComponentsBuilder(),         # Phase 7(可挂接到 RetrievalOrchestrator + ColdPath)
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
