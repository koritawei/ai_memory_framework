"""插件外部依赖绑定 Protocol(设计文档 §2.7.4.3 装配补丁)。

═══════════════════════════════════════════════════════════════════════════════
为什么需要 Bindable Protocol
═══════════════════════════════════════════════════════════════════════════════
SPI 抽象类(如 :class:`EpisodeExtractor`)的契约是「业务能力」(``await extract(...)``),
**不强制**实现方接入外部 client(LLM / Embedding / ES / Milvus)。但很多默认实现
确实需要外部 client,装配层(``deps/builders/*``)在 ``factory.build`` 后必须把
client 注入进去 —— 历史上是反射 ``getattr(obj, "bind_xxx", None)``,无法被
mypy 静态校验。

本模块把每种「注入依赖」声明为一个 ``runtime_checkable`` Protocol。
装配层通过 ``isinstance(target, FooBindable)`` 做静态可见的依赖匹配,
mypy 同时能在每个具体插件类上验证 ``bind_foo`` 的签名。

═══════════════════════════════════════════════════════════════════════════════
约定
═══════════════════════════════════════════════════════════════════════════════
- 所有 Protocol 都标 :func:`typing.runtime_checkable`,允许装配期 isinstance
- ``bind_*`` 方法**同步、无返回值**;复杂初始化交给 :meth:`Plugin.start`
- 实现类可选地以 ``class X(SomeSPI, FooBindable): ...`` 形式继承,让 mypy 在
  「方法签名漂移」时立即报错;不显式继承的鸭子类型实现仍然可被 isinstance 命中
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # 仅类型期可见,运行期不引入循环依赖
    from memory_app.plugins.spi.embedding_provider import EmbeddingProvider
    from memory_app.plugins.spi.entity_extractor import EntityExtractor
    from memory_app.plugins.spi.llm_provider import LLMProvider


# ════════════════════════════════════════════════════════════════════════════
# Provider 类客户端
# ════════════════════════════════════════════════════════════════════════════
@runtime_checkable
class LLMClientBindable(Protocol):
    """支持注入 :class:`LLMProvider` 鸭子类型客户端的插件。

    典型实现:
    - :class:`LLMEpisodeExtractor` / :class:`LLM10AssociationExtractor`
    - :class:`HybridSBD` / :class:`LLMSBD`
    """

    def bind_llm_client(self, client: "LLMProvider") -> None: ...


@runtime_checkable
class EmbeddingClientBindable(Protocol):
    """支持注入 :class:`EmbeddingProvider` 鸭子类型客户端的插件。"""

    def bind_embedding_client(self, client: "EmbeddingProvider") -> None: ...


# ════════════════════════════════════════════════════════════════════════════
# 存储/搜索客户端
# ════════════════════════════════════════════════════════════════════════════
@runtime_checkable
class ESClientBindable(Protocol):
    """支持注入 Elasticsearch ``AsyncElasticsearch`` 客户端的插件(典型:bm25 通道)。"""

    def bind_es_client(self, client: Any) -> None: ...


@runtime_checkable
class MilvusCollectionBindable(Protocol):
    """支持注入 pymilvus ``Collection`` 实例的插件(典型:vector 通道)。"""

    def bind_collection(self, collection: Any) -> None: ...


@runtime_checkable
class MongoRepoBindable(Protocol):
    """支持注入 ``MongoMemCellRepo`` 的插件(典型:entity / graph 通道)。"""

    def bind_mongo_repo(self, repo: Any) -> None: ...


# ════════════════════════════════════════════════════════════════════════════
# Phase 7 图与实体
# ════════════════════════════════════════════════════════════════════════════
@runtime_checkable
class EntityStoreBindable(Protocol):
    """支持注入 ``EntityStore`` 的插件(典型:entity 通道)。"""

    def bind_entity_store(self, store: Any) -> None: ...


@runtime_checkable
class MemoryGraphBindable(Protocol):
    """支持注入 ``MemoryGraph`` 的插件(典型:graph 通道)。"""

    def bind_memory_graph(self, graph: Any) -> None: ...


@runtime_checkable
class EntityExtractorBindable(Protocol):
    """支持注入 :class:`EntityExtractor` 的插件(典型:entity / graph 通道)。"""

    def bind_entity_extractor(self, extractor: "EntityExtractor") -> None: ...


# ════════════════════════════════════════════════════════════════════════════
# 巩固管线
# ════════════════════════════════════════════════════════════════════════════
@runtime_checkable
class PipelineComponentsBindable(Protocol):
    """支持注入 ``sleep`` / ``decay`` 子组件的 ConsolidationStrategy 实现。

    与其它 Bindable 不同,此 bind 是关键字参数;实现方应允许任一为 ``None``
    (Sleep 或 Decay 组件不可用时仍能继续)。
    """

    def bind_pipeline_components(self, *, sleep: Any = None, decay: Any = None) -> None: ...


__all__ = [
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
