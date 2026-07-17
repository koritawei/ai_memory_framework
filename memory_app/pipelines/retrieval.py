"""RetrievalPipeline —— 检索五阶段(设计文档 §2.7.6 / §6)。

═══════════════════════════════════════════════════════════════════════════════
阶段顺序
═══════════════════════════════════════════════════════════════════════════════
::

    RecallStage          (多路并发召回:bm25 + vector + ...)
        ↓
    FuseStage            (Fuser SPI 合并多通道结果 → RRFScore)
        ↓
    SignalBoostStage     (RRFScore × TimeDecay × (1+Imp))
        ↓
    FilterStage          (RetrievalFilter SPI 链:threshold / lifecycle / ...)
        ↓
    RerankStage          (Reranker SPI:mmr → 可选 cross_encoder)

═══════════════════════════════════════════════════════════════════════════════
失败语义
═══════════════════════════════════════════════════════════════════════════════
- 单个通道失败 → ctx.warnings 记录,**继续**剩余通道(可观测但不阻塞)
- Fuser / Reranker / Filter 失败 → 抛(管线对外 500;由路由层包装)
- 所有通道全失败 → 抛 PluginError(retryable=True)

═══════════════════════════════════════════════════════════════════════════════
组件注入(鸭子类型)
═══════════════════════════════════════════════════════════════════════════════
- ``channels``       ``{name: object}``,object 提供 ``await retrieve(query, ctx, k)``
- ``fuser``          ``await fuse(channel_outputs, weights)`` + 可选 ``apply_signal_boost``
- ``filters``        list,链式;每项 ``await filter(candidates, ctx)``
- ``reranker``       ``await rerank(query, candidates, top_k)``
- ``signal_provider`` 可选;``await fetch(memory_ids) -> (time_decays, importances)``
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from memory_app.internal_models import RankedMemory
from memory_app.pipelines.base import BasePipeline, PipelineStage
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.schemas.retrieve import RetrieveMemRequest

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 鸭子类型协议
# ════════════════════════════════════════════════════════════════════════════
class _ChannelProto(Protocol):
    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]: ...


class _FuserProto(Protocol):
    async def fuse(
        self,
        channel_outputs: dict[str, list[RankedMemory]],
        weights: dict[str, float] | None = None,
    ) -> list[RankedMemory]: ...


class _FilterProto(Protocol):
    async def filter(
        self, candidates: list[RankedMemory], ctx: RetrievalContext
    ) -> list[RankedMemory]: ...


class _RerankerProto(Protocol):
    async def rerank(
        self,
        query: str,
        candidates: list[RankedMemory],
        top_k: int | None = None,
    ) -> list[RankedMemory]: ...


_SignalProvider = Callable[
    [list[str]], Awaitable[tuple[dict[str, float], dict[str, float]]]
]


# ════════════════════════════════════════════════════════════════════════════
# 上下文
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class RetrievalPipelineContext:
    """阶段间共享上下文。"""

    request: RetrieveMemRequest

    #: 每路 over-fetch 后的 k(默认 top_k * over_fetch_factor)
    recall_k: int = 40

    #: RecallStage 输出
    channel_outputs: dict[str, list[RankedMemory]] = field(default_factory=dict)
    channel_warnings: dict[str, str] = field(default_factory=dict)  # name → error msg

    #: Fuse 之后的合并候选(单一列表)
    fused: list[RankedMemory] = field(default_factory=list)

    #: 信号增强后的候选
    boosted: list[RankedMemory] = field(default_factory=list)

    #: 过滤后的候选
    filtered: list[RankedMemory] = field(default_factory=list)

    #: 重排后的最终结果
    final: list[RankedMemory] = field(default_factory=list)

    #: debug / 监控
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # ────────────────────────────────────────────────────────────────────────
    @property
    def retrieval_ctx(self) -> RetrievalContext:
        req = self.request
        return RetrievalContext(
            tenant_id=req.tenant_id,
            user_id=req.user_id,
            intent=req.intent.value if req.intent else None,
            filters=req.filters,
        )


# ════════════════════════════════════════════════════════════════════════════
# Stage 1:多路召回
# ════════════════════════════════════════════════════════════════════════════
class RecallStage(PipelineStage[RetrievalPipelineContext]):
    name = "recall"

    def __init__(
        self,
        channels: dict[str, _ChannelProto] | None,
        *,
        timeout_per_channel_s: float = 5.0,
    ) -> None:
        self._channels = dict(channels or {})
        self._timeout = timeout_per_channel_s

    def add_channel(self, name: str, channel: _ChannelProto) -> None:
        """注册一路新通道(供 Phase 7 entity / graph 通道在装配末尾追加使用)。

        重复 name 直接覆盖。**替代旧版** ``recall._channels[name] = channel``
        直接 mutate private 字段的反模式。
        """
        self._channels[name] = channel

    def channel_names(self) -> list[str]:
        """当前注册的通道名(便于装配层日志)。"""
        return list(self._channels.keys())

    async def run(
        self, ctx: RetrievalPipelineContext
    ) -> RetrievalPipelineContext:
        if not self._channels:
            ctx.warnings.append("no_channels_configured")
            return ctx
        # 通道开关:retrieval_config.enabled_channels 优先;空时全部启用
        rc = ctx.request.retrieval_config
        enabled = (
            set(rc.enabled_channels) if rc and rc.enabled_channels else None
        )
        candidates: dict[str, _ChannelProto] = {
            n: c for n, c in self._channels.items() if enabled is None or n in enabled
        }
        if not candidates:
            ctx.warnings.append("no_enabled_channels")
            return ctx

        # 并发召回 + 单路超时
        async def _call(name: str, ch: _ChannelProto) -> tuple[str, Any]:
            try:
                hits = await asyncio.wait_for(
                    ch.retrieve(ctx.request.query, ctx.retrieval_ctx, ctx.recall_k),
                    timeout=self._timeout,
                )
                return name, hits
            except Exception as e:  # noqa: BLE001
                return name, e

        results = await asyncio.gather(
            *[_call(n, c) for n, c in candidates.items()],
            return_exceptions=False,
        )
        ok_count = 0
        for name, value in results:
            if isinstance(value, Exception):
                ctx.channel_warnings[name] = f"{value.__class__.__name__}:{value}"
                logger.warning("channel %s failed: %s", name, value)
                continue
            ctx.channel_outputs[name] = list(value or [])
            ok_count += 1

        # 全失败 → 抛(retryable=True)
        if ok_count == 0:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "all_channels_failed",
                f"all {len(candidates)} channels failed",
                retryable=True,
            )
        ctx.metrics["channels_ok"] = ok_count
        ctx.metrics["channels_failed"] = len(ctx.channel_warnings)
        ctx.metrics["recall_k"] = ctx.recall_k
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Stage 2:融合
# ════════════════════════════════════════════════════════════════════════════
class FuseStage(PipelineStage[RetrievalPipelineContext]):
    name = "fuse"

    def __init__(self, fuser: _FuserProto | None) -> None:
        self._fuser = fuser

    async def run(
        self, ctx: RetrievalPipelineContext
    ) -> RetrievalPipelineContext:
        if self._fuser is None:
            # 无 fuser → 把所有通道结果按 score 拼接,取并集
            ctx.fused = _flat_concat(ctx.channel_outputs)
            ctx.warnings.append("fuser_unbound")
            return ctx
        if not ctx.channel_outputs:
            ctx.fused = []
            return ctx
        ctx.fused = await self._fuser.fuse(ctx.channel_outputs)
        ctx.metrics["fused_count"] = len(ctx.fused)
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Stage 3:信号增强
# ════════════════════════════════════════════════════════════════════════════
class SignalBoostStage(PipelineStage[RetrievalPipelineContext]):
    name = "signal_boost"

    def __init__(
        self,
        fuser: _FuserProto | None,
        *,
        signal_provider: _SignalProvider | None = None,
    ) -> None:
        self._fuser = fuser
        self._signal_provider = signal_provider

    async def run(
        self, ctx: RetrievalPipelineContext
    ) -> RetrievalPipelineContext:
        if not ctx.fused:
            ctx.boosted = []
            return ctx
        # 没有 fuser 或 fuser 没有 apply_signal_boost → 直接传递
        boost_fn = getattr(self._fuser, "apply_signal_boost", None) if self._fuser else None
        if boost_fn is None:
            ctx.boosted = list(ctx.fused)
            return ctx
        # 信号源不可用时退化为 1.0 / 0.0,等价于不增强
        time_decays: dict[str, float] = {}
        importances: dict[str, float] = {}
        if self._signal_provider is not None:
            ids = [h.memory_id for h in ctx.fused]
            try:
                td, imp = await self._signal_provider(ids)
                time_decays = td or {}
                importances = imp or {}
            except Exception as e:  # noqa: BLE001
                ctx.warnings.append(f"signal_provider_failed:{e.__class__.__name__}")
        ctx.boosted = boost_fn(
            list(ctx.fused),
            time_decays=time_decays,
            importances=importances,
        )
        ctx.metrics["boosted_count"] = len(ctx.boosted)
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Stage 4:过滤
# ════════════════════════════════════════════════════════════════════════════
class FilterStage(PipelineStage[RetrievalPipelineContext]):
    name = "filter"

    def __init__(self, filters: list[_FilterProto] | None) -> None:
        self._filters = list(filters or [])

    async def run(
        self, ctx: RetrievalPipelineContext
    ) -> RetrievalPipelineContext:
        candidates = list(ctx.boosted) if ctx.boosted else list(ctx.fused)
        if not self._filters:
            ctx.filtered = candidates
            return ctx
        for f in self._filters:
            try:
                candidates = await f.filter(candidates, ctx.retrieval_ctx)
            except Exception as e:  # noqa: BLE001
                # 单个 filter 失败:记录 warn,跳过(不应让一个过滤器拖垮整条管线)
                ctx.warnings.append(
                    f"filter_failed:{f.__class__.__name__}:{e.__class__.__name__}"
                )
                logger.warning("filter %s failed: %s", f.__class__.__name__, e)
        ctx.filtered = candidates
        ctx.metrics["filtered_count"] = len(candidates)
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Stage 5:重排
# ════════════════════════════════════════════════════════════════════════════
class RerankStage(PipelineStage[RetrievalPipelineContext]):
    name = "rerank"

    def __init__(self, reranker: _RerankerProto | None) -> None:
        self._reranker = reranker

    async def run(
        self, ctx: RetrievalPipelineContext
    ) -> RetrievalPipelineContext:
        candidates = ctx.filtered
        top_k = int(ctx.request.top_k)
        if not candidates:
            ctx.final = []
            return ctx
        if self._reranker is None:
            ctx.final = candidates[:top_k]
            return ctx
        ctx.final = await self._reranker.rerank(
            ctx.request.query, candidates, top_k=top_k
        )
        ctx.metrics["final_count"] = len(ctx.final)
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# 主管线
# ════════════════════════════════════════════════════════════════════════════
class RetrievalPipeline(
    BasePipeline[RetrieveMemRequest, list[RankedMemory], RetrievalPipelineContext]
):
    """五阶段检索主管线。"""

    def __init__(
        self,
        *,
        channels: dict[str, _ChannelProto] | None = None,
        fuser: _FuserProto | None = None,
        filters: list[_FilterProto] | None = None,
        reranker: _RerankerProto | None = None,
        signal_provider: _SignalProvider | None = None,
        over_fetch_factor: int = 4,
        timeout_per_channel_s: float = 5.0,
        extra_stages: list[PipelineStage[RetrievalPipelineContext]] | None = None,
    ) -> None:
        self._recall = RecallStage(channels, timeout_per_channel_s=timeout_per_channel_s)
        self._fuse = FuseStage(fuser)
        self._boost = SignalBoostStage(fuser, signal_provider=signal_provider)
        self._filter = FilterStage(filters)
        self._rerank = RerankStage(reranker)
        self._over_fetch_factor = max(1, int(over_fetch_factor))
        self._extra_stages = list(extra_stages or [])

    def stages(self) -> list[PipelineStage[RetrievalPipelineContext]]:
        return [
            self._recall,
            self._fuse,
            self._boost,
            self._filter,
            self._rerank,
            *self._extra_stages,
        ]

    async def build_context(
        self, input_data: RetrieveMemRequest
    ) -> RetrievalPipelineContext:
        rc = input_data.retrieval_config
        over_fetch = (
            int(rc.over_fetch_factor)
            if rc and rc.over_fetch_factor
            else self._over_fetch_factor
        )
        return RetrievalPipelineContext(
            request=input_data,
            recall_k=max(1, int(input_data.top_k) * over_fetch),
        )

    async def finalize(
        self, ctx: RetrievalPipelineContext
    ) -> list[RankedMemory]:
        # 截断 top_k
        return ctx.final[: ctx.request.top_k]


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def _flat_concat(
    channel_outputs: dict[str, list[RankedMemory]],
) -> list[RankedMemory]:
    """无 fuser 时降级:多路结果按 score 拼接 + 去重(取每个 mem 的最高分)。

    复制策略:shallow ``model_copy()`` —— 本函数只修改 ``rank`` 标量字段,
    不动 metadata / embedding 等共享引用;省去 deep-copy 1024d embedding 的开销。
    """
    seen: dict[str, RankedMemory] = {}
    for hits in channel_outputs.values():
        for h in hits:
            cur = seen.get(h.memory_id)
            if cur is None or h.score > cur.score:
                seen[h.memory_id] = h.model_copy()
    items = list(seen.values())
    items.sort(key=lambda h: h.score, reverse=True)
    for i, h in enumerate(items):
        h.rank = i
    return items


__all__ = [
    "RetrievalPipeline",
    "RetrievalPipelineContext",
    "RecallStage",
    "FuseStage",
    "SignalBoostStage",
    "FilterStage",
    "RerankStage",
]
