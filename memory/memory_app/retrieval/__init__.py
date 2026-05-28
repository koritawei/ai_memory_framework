"""检索管线核心算法集合。

═══════════════════════════════════════════════════════════════════════════════
模块组织
═══════════════════════════════════════════════════════════════════════════════
- :mod:`channels`     单路召回模板 + BM25 / Vector 实现
- :mod:`fusion`       多路融合(RRF + 信号增强)
- :mod:`reranker`     重排(MMR + Cross-Encoder hook)
- :mod:`orchestrator` 五阶段检索编排门面

插件层 :mod:`memory_app.plugins_default` 内的 ``bm25_es_channel`` /
``vector_milvus_channel`` / ``weighted_rrf_fuser`` / ``mmr_reranker`` /
``threshold_filter`` 等,分别是这些核心类的薄包装。
"""

from memory_app.retrieval.channels.base import BaseRetrievalChannel
from memory_app.retrieval.channels.bm25 import BM25Channel
from memory_app.retrieval.channels.vector import VectorChannel
from memory_app.retrieval.fusion import (
    BaseFusion,
    RRFConfig,
    RRFFusion,
    SignalBoost,
)
from memory_app.retrieval.orchestrator import RetrievalOrchestrator
from memory_app.retrieval.reranker import BaseReranker, MMRReranker

__all__ = [
    "BaseRetrievalChannel",
    "BM25Channel",
    "VectorChannel",
    "BaseFusion",
    "RRFConfig",
    "RRFFusion",
    "SignalBoost",
    "BaseReranker",
    "MMRReranker",
    "RetrievalOrchestrator",
]
