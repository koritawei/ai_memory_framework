"""默认插件实现集合（按能力域分组）。

已注册插件
─────────────────────────────────────────────────────────────────────────────
脚手架 / 数据模型
- ``noop_sbd``                stub（永不切边界，保留供回退）
- ``noop_fuser``              stub（直接拼接通道结果，检索默认切到 ``weighted_rrf``）

写入热路径
- ``rule_sbd``                规则 SBD（time_gap + window_turns + window_tokens）

写入冷路径
- ``llm_sbd``                 纯 LLM SBD（对照实验 / 灰度兜底）
- ``hybrid_sbd``              规则优先 + LLM 兜底（冷路径默认）
- ``llm_episode_extractor``   LLM 情景抽取
- ``llm_10_association``      LLM 语义联想 ~10 条
- ``incremental_centroid``    增量质心聚类

检索
- ``bm25_es``                 ES BM25 召回通道
- ``vector_milvus``           Milvus 向量召回通道
- ``weighted_rrf``            加权 RRF 融合（替代 noop_fuser 默认）
- ``mmr``                     Maximal Marginal Relevance 重排
- ``threshold``               阈值过滤

反馈与生命周期
- ``synaptic_plasticity_reinforcer``  反馈强化
- ``ebbinghaus_v1``                   艾宾浩斯遗忘曲线（默认）
- ``fsfm_4d``                         FSFM 四维重要性评分

离线巩固
- ``composite``                       Consolidator 综合相似度
- ``three_phase``                     ConsolidationStrategy 三相睡眠巩固
- ``greedy``                          CapacityOptimizer 贪心容量优化

图与实体
- ``regex_entity_extractor``          正则启发式 EntityExtractor
- ``entity_boost``                    Entity Boost 召回通道
- ``in_memory_lru_graph``             内存 LRU GraphStore
- ``graph_traversal``                 图遍历召回通道

铁律
─────────────────────────────────────────────────────────────────────────────
**业务平面禁止**直接 import 本目录下任何模块；
务必经 :meth:`memory_app.plugins.PluginFactory.build` 取实例。
违反将被 ``scripts/audit_no_hard_deps.py`` 在 CI 中拦截。
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
    incremental_centroid,
    in_memory_lru_graph,
    llm_10_association,
    llm_episode_extractor,
    llm_sbd,
    mmr_reranker,
    noop_fuser,
    noop_sbd,
    regex_entity_extractor,
    rule_sbd,
    synaptic_reinforcer,
    threshold_filter,
    three_phase_dreaming,
    vector_milvus_channel,
    weighted_rrf_fuser,
)

__all__: list[str] = []
