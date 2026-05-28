"""SPI 抽象类集合。

═══════════════════════════════════════════════════════════════════════════════
本目录是「业务平面 ↔ 具体实现」的契约总入口
═══════════════════════════════════════════════════════════════════════════════
30 个扩展点按类别分组：

- **生成（9）**：BoundaryDetector / EpisodeExtractor / SemanticExtractor /
  EventLogExtractor / ProfileExtractor / Clusterer / Consolidator /
  EntityExtractor / ValueDiscriminator
- **检索（6）**：RetrievalChannel / Fuser / Reranker / RetrievalFilter /
  QueryRewriter / IntentClassifier
- **存储（7）**：KVStore / VectorStore / BM25Store / GraphStore / CacheStore /
  IdempotencyStore / DLQStore
- **生命周期（5）**：ForgettingPolicy / ImportanceScorer /
  ConsolidationStrategy / CapacityOptimizer / Reinforcer
- **Provider（3）**：EmbeddingProvider / LLMProvider / RerankProvider

═══════════════════════════════════════════════════════════════════════════════
约定
═══════════════════════════════════════════════════════════════════════════════
- 每个 ABC 继承 :class:`memory_app.plugins.base.Plugin`
- 仅声明抽象方法 + 类型签名，**禁止**写任何业务逻辑
- 每个抽象方法 docstring 含「约定」段（前置 / 后置 / 错误约定）
- 实现位于 ``memory_app/plugins_default/`` 与第三方包
"""

# 生成
from .boundary_detector import (
    BoundaryContext,
    BoundaryDetectionResult,
    BoundaryDetector,
)
from .clusterer import ClusterAssignmentMeta, Clusterer
from .consolidator import ConsolidationDecision, Consolidator, ConsolidatorResult
from .entity_extractor import Entity, EntityExtractor
from .episode_extractor import EpisodeExtractor, ScenarioType
from .event_log_extractor import EventLogExtractor
from .profile_extractor import ProfileExtractor
from .semantic_extractor import SemanticExtractor
from .value_discriminator import ValueDiscriminator, ValueJudgement

# 检索
from .fuser import Fuser
from .intent_classifier import IntentClassifier
from .query_rewriter import QueryRewriter
from .reranker import Reranker
from .retrieval_channel import RetrievalChannel, RetrievalContext
from .retrieval_filter import RetrievalFilter

# 存储
from .bm25_store import BM25Hit, BM25Store
from .cache_store import CacheStore
from .dlq_store import DLQRecord, DLQStore
from .graph_store import GraphEdge, GraphNode, GraphStore
from .idempotency_store import IdempotencyClaim, IdempotencyStore
from .kv_store import KVStore
from .vector_store import VectorHit, VectorItem, VectorStore

# 生命周期
from .capacity_optimizer import CapacityOptimizer
from .consolidation_strategy import ConsolidationReport, ConsolidationStrategy
from .forgetting_policy import ForgettingPolicy, MemoryRef
from .importance_scorer import ImportanceScore, ImportanceScorer
from .reinforcer import Reinforcer

# Provider
from .embedding_provider import EmbeddingProvider
from .llm_provider import LLMProvider
from .rerank_provider import RerankProvider, RerankResult

# 装配期依赖绑定 Protocol(见 bindings.py docstring)
from .bindings import (
    EmbeddingClientBindable,
    EntityExtractorBindable,
    EntityStoreBindable,
    ESClientBindable,
    LLMClientBindable,
    MemoryGraphBindable,
    MilvusCollectionBindable,
    MongoRepoBindable,
    PipelineComponentsBindable,
)

__all__ = [
    # 生成
    "BoundaryDetector", "BoundaryContext", "BoundaryDetectionResult",
    "EpisodeExtractor", "ScenarioType",
    "SemanticExtractor",
    "EventLogExtractor",
    "ProfileExtractor",
    "Clusterer", "ClusterAssignmentMeta",
    "Consolidator", "ConsolidationDecision", "ConsolidatorResult",
    "EntityExtractor", "Entity",
    "ValueDiscriminator", "ValueJudgement",
    # 检索
    "RetrievalChannel", "RetrievalContext",
    "Fuser",
    "Reranker",
    "RetrievalFilter",
    "QueryRewriter",
    "IntentClassifier",
    # 存储
    "KVStore",
    "VectorStore", "VectorItem", "VectorHit",
    "BM25Store", "BM25Hit",
    "GraphStore", "GraphNode", "GraphEdge",
    "CacheStore",
    "IdempotencyStore", "IdempotencyClaim",
    "DLQStore", "DLQRecord",
    # 生命周期
    "ForgettingPolicy", "MemoryRef",
    "ImportanceScorer", "ImportanceScore",
    "ConsolidationStrategy", "ConsolidationReport",
    "CapacityOptimizer",
    "Reinforcer",
    # Provider
    "EmbeddingProvider",
    "LLMProvider",
    "RerankProvider", "RerankResult",
    # 装配依赖绑定 Protocol
    "LLMClientBindable",
    "EmbeddingClientBindable",
    "ESClientBindable",
    "MilvusCollectionBindable",
    "MongoRepoBindable",
    "EntityStoreBindable",
    "MemoryGraphBindable",
    "EntityExtractorBindable",
    "PipelineComponentsBindable",
]
