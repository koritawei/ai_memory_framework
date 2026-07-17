"""管线编排框架(设计文档 §2.7.6)。

═══════════════════════════════════════════════════════════════════════════════
三条管线统一为「阶段链」编排
═══════════════════════════════════════════════════════════════════════════════
- :class:`IngestPipeline`     写入热路径(Phase 2)
- ``ColdPathPipeline``        异步冷路径(Phase 3)
- ``RetrievalPipeline``       检索路径(Phase 4)

业务门面(``IngestService`` / ``ColdPathService`` / ``RetrievalOrchestrator``)
**委托** ``execute()``,不在门面内展开阶段逻辑;新增阶段优先插入 ``stages()``
列表,而非修改路由。
"""

from .base import BasePipeline, PipelineStage
from .cold_path import (
    ClusterStage,
    ColdPathContext,
    ColdPathPipeline,
    EntityIndexStage,
    EpisodeExtractStage,
    SemanticExtractStage,
)
from .ingest import (
    IngestPipeline,
    IngestPipelineContext,
    PersistMemCellStage,
    SegmentStage,
    SyncIndexStage,
)
from .retrieval import (
    FilterStage,
    FuseStage,
    RecallStage,
    RerankStage,
    RetrievalPipeline,
    RetrievalPipelineContext,
    SignalBoostStage,
)

__all__ = [
    "BasePipeline",
    "PipelineStage",
    # Phase 2 写入热路径
    "IngestPipeline",
    "IngestPipelineContext",
    "SegmentStage",
    "PersistMemCellStage",
    "SyncIndexStage",
    # Phase 3 写入冷路径
    "ColdPathPipeline",
    "ColdPathContext",
    "EpisodeExtractStage",
    "SemanticExtractStage",
    "ClusterStage",
    # Phase 7 实体 / 图索引
    "EntityIndexStage",
    # Phase 4 检索
    "RetrievalPipeline",
    "RetrievalPipelineContext",
    "RecallStage",
    "FuseStage",
    "SignalBoostStage",
    "FilterStage",
    "RerankStage",
]
