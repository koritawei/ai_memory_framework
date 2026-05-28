"""DependencyBinder —— 把外部 client / store 注入插件实例。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:meth:`PluginFactory.build` 只负责"按 ConfigCenter 的 name 拿到类并 start"。
但很多默认插件在 :meth:`Plugin.start` 之后还需要外部资源(LLM client / ES /
Milvus / EntityStore / MemoryGraph...)才能真正工作。

历史上装配层(``deps.py``)用 13 处反射 ``getattr(obj, "bind_xxx", None)``
完成这件事。本类把它收口为统一工具,基于
:mod:`memory_app.plugins.spi.bindings` 的 ``Protocol`` 做 isinstance 分派,
让 mypy 能静态检查"哪个 bind 真的被支持"。

═══════════════════════════════════════════════════════════════════════════════
使用模式
═══════════════════════════════════════════════════════════════════════════════
.. code-block:: python

    binder = DependencyBinder(
        llm_client=llm_provider,
        embedding_client=embedding_provider,
        es_client=app_state.es_client,
        milvus_collection=collection,
        entity_store=app_state.entity_store,
        memory_graph=app_state.memory_graph,
        entity_extractor=app_state.entity_extractor,
        mongo_repo=app_state.mongo_repo,
    )

    binder.bind(extractor)         # 任意插件实例,自动按 Protocol 匹配
    binder.bind(channel)
    binder.bind_pipeline_components(strategy, sleep=sleep, decay=decay)

═══════════════════════════════════════════════════════════════════════════════
设计要点
═══════════════════════════════════════════════════════════════════════════════
- ``None`` 依赖被静默跳过 —— 装配层无须先判空再注入
- 所有匹配走 ``isinstance(x, FooBindable)`` —— 与 ``runtime_checkable`` Protocol
  完美配合;鸭子类型实现(未显式继承)也能命中
- ``bind`` 是幂等的:多次调用相同 binder.bind(target) 行为等价于单次调用
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from memory_app.plugins.spi.bindings import (
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

if TYPE_CHECKING:
    from memory_app.plugins.spi.embedding_provider import EmbeddingProvider
    from memory_app.plugins.spi.entity_extractor import EntityExtractor
    from memory_app.plugins.spi.llm_provider import LLMProvider

logger = logging.getLogger(__name__)


class DependencyBinder:
    """把外部 client / store 按 Protocol 匹配的方式注入插件实例。

    依赖项均为可选 —— 装配层只填它实际拿到的东西,bind 时未提供的依赖会被静默跳过。
    """

    def __init__(
        self,
        *,
        llm_client: "LLMProvider | None" = None,
        embedding_client: "EmbeddingProvider | None" = None,
        es_client: Any | None = None,
        milvus_collection: Any | None = None,
        mongo_repo: Any | None = None,
        entity_store: Any | None = None,
        memory_graph: Any | None = None,
        entity_extractor: "EntityExtractor | None" = None,
    ) -> None:
        self.llm_client = llm_client
        self.embedding_client = embedding_client
        self.es_client = es_client
        self.milvus_collection = milvus_collection
        self.mongo_repo = mongo_repo
        self.entity_store = entity_store
        self.memory_graph = memory_graph
        self.entity_extractor = entity_extractor

    # ════════════════════════════════════════════════════════════════════════
    # 主入口
    # ════════════════════════════════════════════════════════════════════════
    def bind(self, target: Any) -> list[str]:
        """对 ``target`` 应用所有可匹配 Protocol 的注入。

        :returns: 实际触发的 bind 名称列表(便于装配层 logger.info 追溯)
        """
        applied: list[str] = []
        if self.llm_client is not None and isinstance(target, LLMClientBindable):
            target.bind_llm_client(self.llm_client)
            applied.append("llm_client")
        if self.embedding_client is not None and isinstance(target, EmbeddingClientBindable):
            target.bind_embedding_client(self.embedding_client)
            applied.append("embedding_client")
        if self.es_client is not None and isinstance(target, ESClientBindable):
            target.bind_es_client(self.es_client)
            applied.append("es_client")
        if self.milvus_collection is not None and isinstance(target, MilvusCollectionBindable):
            target.bind_collection(self.milvus_collection)
            applied.append("milvus_collection")
        if self.mongo_repo is not None and isinstance(target, MongoRepoBindable):
            target.bind_mongo_repo(self.mongo_repo)
            applied.append("mongo_repo")
        if self.entity_store is not None and isinstance(target, EntityStoreBindable):
            target.bind_entity_store(self.entity_store)
            applied.append("entity_store")
        if self.memory_graph is not None and isinstance(target, MemoryGraphBindable):
            target.bind_memory_graph(self.memory_graph)
            applied.append("memory_graph")
        if self.entity_extractor is not None and isinstance(target, EntityExtractorBindable):
            target.bind_entity_extractor(self.entity_extractor)
            applied.append("entity_extractor")
        return applied

    # ════════════════════════════════════════════════════════════════════════
    # 特殊形态:关键字参数的 bind_pipeline_components
    # ════════════════════════════════════════════════════════════════════════
    @staticmethod
    def bind_pipeline_components(
        target: Any, *, sleep: Any = None, decay: Any = None
    ) -> bool:
        """单独暴露 ``bind_pipeline_components`` —— 因签名带关键字参数,
        与统一 ``bind(target)`` 形态不兼容,故独立成方法。

        :returns: 是否真的触发了 bind
        """
        if isinstance(target, PipelineComponentsBindable):
            target.bind_pipeline_components(sleep=sleep, decay=decay)
            return True
        return False


__all__ = ["DependencyBinder"]
