"""默认插件实现集合(按 Phase 顺序逐步补齐)。

已注册插件
─────────────────────────────────────────────────────────────────────────────
Phase 0/1
- ``noop_sbd``                Phase 0 stub(永不切边界,保留供回退)
- ``noop_fuser``              Phase 0 stub(直接拼接通道结果,Phase 4 切到 ``weighted_rrf``)

Phase 2
- ``rule_sbd``                规则 SBD(time_gap + window_turns + window_tokens)

Phase 3
- ``llm_sbd``                 纯 LLM SBD(对照实验 / 灰度兜底)
- ``hybrid_sbd``              规则优先 + LLM 兜底(Phase 3 默认)
- ``llm_episode_extractor``   LLM 情景抽取(Step 3.2)
- ``llm_10_association``      LLM 语义联想 ~10 条(Step 3.3)
- ``incremental_centroid``    增量质心聚类(Step 3.4)

Phase 4
- ``bm25_es``                 ES BM25 召回通道(Step 4.1)
- ``vector_milvus``           Milvus 向量召回通道(Step 4.2)
- ``weighted_rrf``            加权 RRF 融合(Step 4.3,替代 noop_fuser 默认)
- ``mmr``                     Maximal Marginal Relevance 重排(Step 4.4)
- ``threshold``               阈值过滤(Step 4.4)

Phase 5
- ``synaptic_plasticity_reinforcer``  反馈强化(Step 5.1)
- ``ebbinghaus_v1``                   艾宾浩斯遗忘曲线(Phase 5 默认)
- ``fsfm_4d``                         FSFM 四维重要性评分(Step 5.3)

Phase 6
- ``composite``                       Consolidator 综合相似度(Step 6.1)
- ``three_phase``                     ConsolidationStrategy 三相睡眠巩固(Step 6.2)
- ``greedy``                          CapacityOptimizer 贪心容量优化(Step 6.3)

Phase 7
- ``regex_entity_extractor``          正则启发式 EntityExtractor(Step 7.1)
- ``entity_boost``                    Entity Boost 召回通道(Step 7.2)
- ``in_memory_lru_graph``             内存 LRU GraphStore(Step 7.3)
- ``graph_traversal``                 图遍历召回通道(Step 7.4)

铁律(设计文档 A.0)
─────────────────────────────────────────────────────────────────────────────
**业务平面禁止**直接 import 本目录下任何模块;
务必经 :meth:`memory_app.plugins.PluginFactory.build` 取实例。
违反将被 Phase 8 Step 8.1 的 ``scripts/audit_no_hard_deps.py`` 在 CI 中拦截。
"""

# import 即触发 @register 装饰器登记到全局 registry
from . import (  # noqa: F401
    bm25_es_channel,
    composite_consolidator,
    ebbinghaus_policy,
    entity_boost_channel,
    fsfm_scorer,
    graph_traversal_channel,
    greedy_capacity_optimizer,
    hybrid_sbd,
    in_memory_lru_graph,
    incremental_centroid,
    llm_10_association,
    llm_episode_extractor,
    llm_sbd,
    mmr_reranker,
    noop_fuser,
    noop_sbd,
    regex_entity_extractor,
    rule_sbd,
    synaptic_reinforcer,
    three_phase_dreaming,
    threshold_filter,
    vector_milvus_channel,
    weighted_rrf_fuser,
)

__all__: list[str] = []
