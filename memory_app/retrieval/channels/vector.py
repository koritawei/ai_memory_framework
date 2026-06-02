"""VectorChannel —— Milvus 向量语义检索通道。

═══════════════════════════════════════════════════════════════════════════════
流程
═══════════════════════════════════════════════════════════════════════════════
1. 调 ``embedding_client.embed([query])`` 拿 query 向量
2. 调 ``collection.search(...)`` 在 Milvus 内做近似 ANN
3. 解析为 :class:`RankedMemory` 列表

═══════════════════════════════════════════════════════════════════════════════
依赖注入
═══════════════════════════════════════════════════════════════════════════════
- ``collection``         任意鸭子类型;``search(data, anns_field, param, limit, expr, output_fields)``
                         同步或异步均可,本类用 :func:`_maybe_await` 兼容
- ``embedding_client``   任意鸭子类型;``await embed(list[str]) -> list[list[float]]``

测试时用 ``MagicMock`` / fake 类即可,生产经
:class:`PluginFactory.build("memory.provider.embedding")` 注入。

═══════════════════════════════════════════════════════════════════════════════
metric_type 约定
═══════════════════════════════════════════════════════════════════════════════
默认 ``COSINE``;Milvus COSINE 返回的是相似度(越大越相关),直接当 score。
若集群配置 ``IP`` / ``L2``,通过 ``metric_type`` 构造参数覆盖,score 含义对齐
即可(MMR / 阈值过滤的 "越大越好" 语义不变)。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.retrieval.channels.base import BaseRetrievalChannel
from memory_app.security.sanitize import escape_milvus_expr_string

logger = logging.getLogger(__name__)

_MILVUS_FILTER_FIELD_ALLOWLIST = frozenset({"memory_type", "state"})


class VectorChannel(BaseRetrievalChannel):
    """Milvus 向量召回。"""

    channel_name = "vector"

    def __init__(
        self,
        collection: Any | None = None,
        embedding_client: Any | None = None,
        *,
        anns_field: str = "embedding",
        metric_type: str = "COSINE",
        nprobe: int = 16,
        over_fetch_factor: int = 4,
        output_fields: list[str] | None = None,
    ) -> None:
        self.collection = collection
        self.embedding_client = embedding_client
        self.anns_field = anns_field
        self.metric_type = metric_type
        self.nprobe = max(1, int(nprobe))
        self.over_fetch_factor = max(1, int(over_fetch_factor))
        self.output_fields = list(
            output_fields or ["mem_cell_id", "text", "memory_type"]
        )

    # ────────────────────────────────────────────────────────────────────────
    # 依赖
    # ────────────────────────────────────────────────────────────────────────
    def _check_dependencies(self) -> None:
        if self.collection is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "milvus_collection_unset",
                "VectorChannel: collection not set",
                retryable=True,
            )
        if self.embedding_client is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "embedding_client_unset",
                "VectorChannel: embedding_client not set",
                retryable=True,
            )

    # ────────────────────────────────────────────────────────────────────────
    # 调用 Milvus
    # ────────────────────────────────────────────────────────────────────────
    async def _execute_search(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int,
        filters: dict[str, Any],
    ) -> Any:
        # 1. embed query
        embeddings = await self.embedding_client.embed([query])
        if not embeddings or not embeddings[0]:
            raise PluginError(
                PluginErrorCategory.INTERNAL,
                "empty_embedding",
                "VectorChannel: embedding_client returned empty embedding",
                retryable=False,
            )
        q_vec = list(embeddings[0])

        # 2. 构造表达式 —— 字符串值经白名单校验 + 转义
        expr_parts = [
            _milvus_eq("tenant_id", tenant_id),
            _milvus_eq("user_id", user_id),
        ]
        for k, v in filters.items():
            if v is None:
                continue
            if k not in _MILVUS_FILTER_FIELD_ALLOWLIST:
                logger.warning("vector channel ignoring disallowed filter key: %s", k)
                continue
            if isinstance(v, str):
                expr_parts.append(_milvus_eq(k, v))
            elif isinstance(v, bool):
                expr_parts.append(f"{k} == {str(v).lower()}")
            elif isinstance(v, (int, float)):
                expr_parts.append(f"{k} == {v}")
        expr = " and ".join(expr_parts)

        # 3. search —— pymilvus Collection.search 是**阻塞**网络 I/O,直接 await
        #    一个同步返回值不能让出事件循环;高 QPS 下会冻结整个 FastAPI worker。
        #    用 asyncio.to_thread 推到线程池,但允许鸭子类型注入 async 实现:
        #    若返回 coroutine,_maybe_await 直接 await。
        params = {"metric_type": self.metric_type, "params": {"nprobe": self.nprobe}}
        limit = top_k * self.over_fetch_factor

        def _run_search() -> Any:
            return self.collection.search(
                data=[q_vec],
                anns_field=self.anns_field,
                param=params,
                limit=limit,
                expr=expr,
                output_fields=self.output_fields,
            )

        # 真实 pymilvus:直接 to_thread,**不能**先 sniff —— 否则 sniff 这一次
        # 调用已在事件循环上阻塞执行了一次 search(且 to_thread 还会再跑第二次),
        # 一次请求触发两次 ANN 搜索 + 完全抵消 to_thread 的解耦效果。
        if _looks_blocking(self.collection):
            return await asyncio.to_thread(_run_search)
        # 非 pymilvus(测试 mock / 鸭子 async 实现):cheap sniff,根据返回值决定是否 await
        sniff = _run_search()
        if inspect.isawaitable(sniff):
            return await sniff
        return sniff

    # ────────────────────────────────────────────────────────────────────────
    # 解析
    # ────────────────────────────────────────────────────────────────────────
    def _parse_hits(self, raw: Any) -> list[RankedMemory]:
        # Milvus 返回 list[list[Hit]] —— 每个 query 一行
        if not raw:
            return []
        try:
            first = raw[0]
        except (IndexError, TypeError):
            return []
        out: list[RankedMemory] = []
        for h in first:
            entity = getattr(h, "entity", None)
            mem_id = _entity_get(entity, "mem_cell_id") or getattr(h, "id", "") or ""
            text = _entity_get(entity, "text") or ""
            mtype = _entity_get(entity, "memory_type")
            try:
                score = float(getattr(h, "distance", 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            md: dict[str, Any] = {}
            if entity is not None:
                for f in self.output_fields:
                    if f in ("mem_cell_id", "text"):
                        continue
                    val = _entity_get(entity, f)
                    if val is not None:
                        md[f] = val
            out.append(
                RankedMemory(
                    memory_id=str(mem_id),
                    memory_type=_normalize_memory_type(mtype) or MemoryType.EPISODIC,
                    content=str(text),
                    score=score,
                    source_channel=self.channel_name,
                    metadata=md,
                )
            )
        return out


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _looks_blocking(collection: Any) -> bool:
    """判断 ``collection`` 是不是真实 pymilvus 客户端(需要线程池)还是测试 mock。

    - pymilvus.Collection 的模块路径以 ``pymilvus`` 开头
    - 测试用 ``MagicMock`` / ``AsyncMock`` 走另外的模块 → 返回 False,直接同步路径
    """
    mod = type(collection).__module__ or ""
    return mod.startswith("pymilvus")


def _milvus_eq(field: str, value: str) -> str:
    """构造 ``field == "value"`` 表达式片段（字段与值均校验）。"""
    safe_field = escape_milvus_expr_string(field)
    safe_value = escape_milvus_expr_string(str(value))
    return f'{safe_field} == "{safe_value}"'


def _escape_milvus_str(value: Any) -> str:
    """校验并转义 Milvus 表达式字符串字面量（统一 security.sanitize 实现）。"""
    try:
        return escape_milvus_expr_string(str(value))
    except ValueError as exc:
        raise PluginError(
            PluginErrorCategory.CONFIG,
            "invalid_milvus_filter",
            f"VectorChannel: invalid filter value {value!r}",
            retryable=False,
        ) from exc


def _entity_get(entity: Any, key: str) -> Any:
    """兼容 pymilvus.Hit.entity 与 dict 两种结构。"""
    if entity is None:
        return None
    if hasattr(entity, "get"):
        try:
            return entity.get(key)
        except TypeError:
            return entity.get(key, None)  # type: ignore[call-arg]
    return getattr(entity, key, None)


def _normalize_memory_type(value: Any) -> MemoryType | None:
    if not value:
        return None
    try:
        return MemoryType(str(value).strip().upper())
    except ValueError:
        return None


__all__ = ["VectorChannel"]
