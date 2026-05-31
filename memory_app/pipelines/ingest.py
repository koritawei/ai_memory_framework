"""IngestPipeline —— 写入热路径管线。

═══════════════════════════════════════════════════════════════════════════════
阶段顺序(写入热路径 完整版)
═══════════════════════════════════════════════════════════════════════════════

::

    SegmentStage          (SBD 切边界,)
        ↓
    PersistMemCellStage   (MongoDB 落 SOT,)
        ↓
    SyncIndexStage        (ES + Milvus 同步,失败入 DLQ;)

新增阶段(冷路径触发 / 准入门 / 鉴权)只需在 ``stages`` 列表中插入,
不影响 :class:`memory_app.services.IngestService` 与路由层。

═══════════════════════════════════════════════════════════════════════════════
SyncIndexStage 失败语义( + )
═══════════════════════════════════════════════════════════════════════════════
- ES 失败 → 记入 DLQ(target=es),**不**抛异常,主路径继续
- Milvus 失败 → 记入 DLQ(target=milvus),**不**抛异常
- 没有 embedding 的 MemCell → 跳过 Milvus 写入(不入 DLQ)
- ES / Milvus repo 为 None(运维关闭) → 跳过对应步骤
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from memory_app.internal_models import MemCell, RawData
from memory_app.pipelines.base import BasePipeline, PipelineStage
from memory_app.repositories.dlq import DLQRecord

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 鸭子类型协议
# ════════════════════════════════════════════════════════════════════════════
class _SegmenterProto(Protocol):
    """SBD 实现需提供 :meth:`segment` 批量切分;契约见 RuleSBD。"""

    async def segment(self, raw_data_list: list[RawData]) -> list[list[RawData]]: ...


class _MemCellRepoProto(Protocol):
    """:class:`MongoMemCellRepo` 协议。"""

    async def insert(self, cell: MemCell) -> str: ...

    #: 可选批量接口;无则 PersistMemCellStage 退化到逐条 insert
    async def insert_many(self, cells: Iterable[MemCell]) -> list[str]: ...


class _ESRepoProto(Protocol):
    """:class:`ESMemCellRepo` 协议。"""

    async def index(self, cell: MemCell) -> None: ...

    #: 可选批量接口;返回 ``{失败 id: 错误字符串}``;无则降级到并发 N 次 index
    async def bulk_index(self, cells: list[MemCell]) -> dict[str, str]: ...


class _MilvusRepoProto(Protocol):
    """:class:`MilvusMemCellRepo` 协议。"""

    async def insert(
        self, mem_cell_id: str, embedding: list[float], metadata: dict[str, Any] | None
    ) -> None: ...

    #: 可选批量接口;返回 ``{失败 id: 错误字符串}``
    async def bulk_insert(
        self,
        records: list[tuple[str, list[float], dict[str, Any] | None]],
    ) -> dict[str, str]: ...


class _DLQProto(Protocol):
    async def enqueue(self, record: DLQRecord) -> None: ...


# ════════════════════════════════════════════════════════════════════════════
# 上下文
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class IngestPipelineContext:
    """IngestPipeline 阶段间共享的上下文。"""

    #: 原始输入(不可修改)
    raw_data_list: list[RawData]

    #: SegmentStage 输出:SBD 切完的 segments
    segments: list[list[RawData]] = field(default_factory=list)

    #: PersistMemCellStage 输出:落库后的 MemCell 列表
    cells: list[MemCell] = field(default_factory=list)

    #: 同步索引阶段结果汇报
    es_failures: list[str] = field(default_factory=list)
    milvus_failures: list[str] = field(default_factory=list)

    #: 各阶段调试 metrics(可选,业务面不依赖)
    metrics: dict[str, Any] = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
# Stage 1: 切边界
# ════════════════════════════════════════════════════════════════════════════
class SegmentStage(PipelineStage[IngestPipelineContext]):
    """调用 SBD 把 raws 切成多段 segments。"""

    name = "segment"

    def __init__(self, segmenter: _SegmenterProto) -> None:
        self._segmenter = segmenter

    async def run(self, ctx: IngestPipelineContext) -> IngestPipelineContext:
        ctx.segments = await self._segmenter.segment(ctx.raw_data_list)
        ctx.metrics["segment_count"] = len(ctx.segments)
        ctx.metrics["raw_count"] = len(ctx.raw_data_list)
        logger.debug(
            "segment: %d raws → %d segments",
            len(ctx.raw_data_list), len(ctx.segments),
        )
        return ctx


# ════════════════════════════════════════════════════════════════════════════
# Stage 2: 落 SOT(MongoDB)
# ════════════════════════════════════════════════════════════════════════════
class PersistMemCellStage(PipelineStage[IngestPipelineContext]):
    """把每个 segment 包装为 MemCell 并写入 MongoDB。"""

    name = "persist_memcell"

    def __init__(self, repo: _MemCellRepoProto) -> None:
        self._repo = repo

    async def run(self, ctx: IngestPipelineContext) -> IngestPipelineContext:
        # 性能:先把所有 segment 包装成 MemCell,再一次 insert_many,
        # 把原 N 次 Mongo round-trip 降为 1 次。
        cells = [self._build_cell(seg) for seg in ctx.segments if seg]
        if cells:
            insert_many = getattr(self._repo, "insert_many", None)
            if callable(insert_many):
                await insert_many(cells)
            else:
                # 兼容仅实现 insert 的 fake repo / 第三方实现
                for c in cells:
                    await self._repo.insert(c)
        ctx.cells.extend(cells)
        ctx.metrics["persisted_count"] = len(ctx.cells)
        return ctx

    @staticmethod
    def _build_cell(segment: list[RawData]) -> MemCell:
        """从 segment 构造 MemCell;text 拼接所有 turns。

        其它字段(``embedding`` / ``summary`` / ``episode``)由 冷路径 冷路径填充,
        写入热路径 仅落最小集合(text + 溯源 ids + 时间戳)。
        """
        first = segment[0]
        text = "\n".join(r.content for r in segment)
        return MemCell(
            tenant_id=first.tenant_id,
            user_id=first.user_id,
            session_id=first.session_id,
            raw_data_ids=[r.raw_id for r in segment],
            text=text,
            timestamp=first.event_time,
        )


# ════════════════════════════════════════════════════════════════════════════
# Stage 3: 同步从属索引(ES + Milvus),失败入 DLQ
# ════════════════════════════════════════════════════════════════════════════
class SyncIndexStage(PipelineStage[IngestPipelineContext]):
    """把 ctx.cells 同步到 ES(BM25)+ Milvus(向量)。

    失败语义:
    - 任一从属索引失败,**不**抛异常 / **不**回滚 SOT,记入 DLQ
    - 没有 embedding 的 MemCell 跳过 Milvus 写入(不入 DLQ —— 这不是失败)
    - ES / Milvus repo 为 None 时整体跳过(运维关停)
    """

    name = "sync_index"

    def __init__(
        self,
        *,
        es_repo: _ESRepoProto | None = None,
        milvus_repo: _MilvusRepoProto | None = None,
        dlq: _DLQProto | None = None,
    ) -> None:
        self._es_repo = es_repo
        self._milvus_repo = milvus_repo
        self._dlq = dlq

    @property
    def es_repo(self) -> _ESRepoProto | None:
        return self._es_repo

    @property
    def milvus_repo(self) -> _MilvusRepoProto | None:
        return self._milvus_repo

    async def run(self, ctx: IngestPipelineContext) -> IngestPipelineContext:
        # 性能优先策略:
        # 1. ES 支持 bulk_index → 一次 Bulk API
        # 2. Milvus 支持 bulk_insert → 一次 Collection.insert(batch)
        # 3. 两路 bulk **互相独立**,asyncio.gather 并发
        # 4. repo 不支持 bulk → 退化到旧的 per-cell 并发(双层 gather)
        if not ctx.cells:
            return ctx

        await asyncio.gather(
            self._sync_es_batch(ctx),
            self._sync_milvus_batch(ctx),
        )

        ctx.metrics["es_failures"] = len(ctx.es_failures)
        ctx.metrics["milvus_failures"] = len(ctx.milvus_failures)
        return ctx

    # ────────────────────────────────────────────────────────────────────────
    # ES 批量(优先) / per-cell 回退
    # ────────────────────────────────────────────────────────────────────────
    async def _sync_es_batch(self, ctx: IngestPipelineContext) -> None:
        if self._es_repo is None:
            return
        bulk_fn = getattr(self._es_repo, "bulk_index", None)
        if callable(bulk_fn):
            try:
                failures = await bulk_fn(list(ctx.cells))
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "ES bulk_index failed for %d cells (all → DLQ): %s",
                    len(ctx.cells), e,
                )
                err_msg = str(e)
                for cell in ctx.cells:
                    ctx.es_failures.append(cell.mem_cell_id)
                    await self._enqueue_dlq(
                        "es",
                        cell.mem_cell_id,
                        err_msg,
                        tenant_id=cell.tenant_id,
                        user_id=cell.user_id,
                    )
                return
            # Bulk 部分失败:把失败 id + 原始错误信息落 DLQ
            cell_by_id = {c.mem_cell_id: c for c in ctx.cells}
            for mid, err_msg in (failures or {}).items():
                ctx.es_failures.append(mid)
                cell = cell_by_id.get(mid)
                await self._enqueue_dlq(
                    "es",
                    mid,
                    err_msg,
                    tenant_id=cell.tenant_id if cell else None,
                    user_id=cell.user_id if cell else None,
                )
            return
        # 回退:per-cell 并发
        await asyncio.gather(*(self._sync_es(c, ctx) for c in ctx.cells))

    # ────────────────────────────────────────────────────────────────────────
    # Milvus 批量(优先) / per-cell 回退
    # ────────────────────────────────────────────────────────────────────────
    async def _sync_milvus_batch(self, ctx: IngestPipelineContext) -> None:
        if self._milvus_repo is None:
            return
        # 过滤掉无 embedding 的 cell(写入热路径 多数 cell 在 cold path 才补 embedding)
        rows = [
            (
                c.mem_cell_id,
                list(c.embedding),
                {"tenant_id": c.tenant_id, "user_id": c.user_id},
            )
            for c in ctx.cells
            if c.embedding
        ]
        if not rows:
            return

        bulk_fn = getattr(self._milvus_repo, "bulk_insert", None)
        if callable(bulk_fn):
            try:
                failures = await bulk_fn(rows)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Milvus bulk_insert failed for %d cells (all → DLQ): %s",
                    len(rows), e,
                )
                err_msg = str(e)
                cell_by_id = {c.mem_cell_id: c for c in ctx.cells}
                for mid, _, _ in rows:
                    ctx.milvus_failures.append(mid)
                    cell = cell_by_id.get(mid)
                    await self._enqueue_dlq(
                        "milvus",
                        mid,
                        err_msg,
                        tenant_id=cell.tenant_id if cell else None,
                        user_id=cell.user_id if cell else None,
                    )
                return
            cell_by_id = {c.mem_cell_id: c for c in ctx.cells}
            for mid, err_msg in (failures or {}).items():
                ctx.milvus_failures.append(mid)
                cell = cell_by_id.get(mid)
                await self._enqueue_dlq(
                    "milvus",
                    mid,
                    err_msg,
                    tenant_id=cell.tenant_id if cell else None,
                    user_id=cell.user_id if cell else None,
                )
            return
        # 回退:per-cell 并发
        await asyncio.gather(*(self._sync_milvus(c, ctx) for c in ctx.cells))

    # ────────────────────────────────────────────────────────────────────────
    async def _sync_es(self, cell: MemCell, ctx: IngestPipelineContext) -> None:
        if self._es_repo is None:
            return
        try:
            await self._es_repo.index(cell)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ES sync failed for %s (degraded → DLQ): %s", cell.mem_cell_id, e
            )
            ctx.es_failures.append(cell.mem_cell_id)
            await self._enqueue_dlq(
                "es",
                cell.mem_cell_id,
                str(e),
                tenant_id=cell.tenant_id,
                user_id=cell.user_id,
            )

    async def _sync_milvus(self, cell: MemCell, ctx: IngestPipelineContext) -> None:
        if self._milvus_repo is None:
            return
        if not cell.embedding:
            # 写入热路径 冷路径未生成 embedding 时跳过 —— 这是预期路径,不入 DLQ
            return
        try:
            await self._milvus_repo.insert(
                cell.mem_cell_id,
                cell.embedding,
                metadata={
                    "tenant_id": cell.tenant_id,
                    "user_id": cell.user_id,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Milvus sync failed for %s (degraded → DLQ): %s",
                cell.mem_cell_id, e,
            )
            ctx.milvus_failures.append(cell.mem_cell_id)
            await self._enqueue_dlq(
                "milvus",
                cell.mem_cell_id,
                str(e),
                tenant_id=cell.tenant_id,
                user_id=cell.user_id,
            )

    async def _enqueue_dlq(
        self,
        target: str,
        mem_cell_id: str,
        err: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if self._dlq is None:
            return
        extra: dict[str, str] | None = None
        if tenant_id and user_id:
            extra = {"tenant_id": tenant_id, "user_id": user_id}
        try:
            await self._dlq.enqueue(
                DLQRecord(
                    target=target,
                    mem_cell_id=mem_cell_id,
                    operation="index",
                    error=err,
                    extra=extra,
                )
            )
        except Exception as e:  # noqa: BLE001
            # DLQ 自身故障也不应阻塞主路径;再次降级到 log
            logger.error(
                "DLQ enqueue failed (target=%s id=%s): %s",
                target, mem_cell_id, e,
            )


# ════════════════════════════════════════════════════════════════════════════
# 主管线
# ════════════════════════════════════════════════════════════════════════════
class IngestPipeline(
    BasePipeline[list[RawData], list[str], IngestPipelineContext]
):
    """写入热路径主管线。

    ``execute(raw_data_list) -> list[mem_cell_id]``
    """

    def __init__(
        self,
        *,
        segmenter: _SegmenterProto,
        mem_cell_repo: _MemCellRepoProto,
        es_repo: _ESRepoProto | None = None,
        milvus_repo: _MilvusRepoProto | None = None,
        dlq: _DLQProto | None = None,
        extra_stages: list[PipelineStage[IngestPipelineContext]] | None = None,
    ) -> None:
        self._segment_stage = SegmentStage(segmenter)
        self._persist_stage = PersistMemCellStage(mem_cell_repo)
        # SyncIndexStage 始终插入,内部据 repo 为 None 决定是否真做事;
        # 让"关闭 ES 同步"不需要重组 stages 列表
        self._sync_stage = SyncIndexStage(
            es_repo=es_repo, milvus_repo=milvus_repo, dlq=dlq
        )
        self._extra_stages: list[PipelineStage[IngestPipelineContext]] = list(
            extra_stages or []
        )

    # ════════════════════════════════════════════════════════════════════════
    # BasePipeline 契约
    # ════════════════════════════════════════════════════════════════════════
    def stages(self) -> list[PipelineStage[IngestPipelineContext]]:
        # 顺序:SBD → 落 MongoDB → 同步从属索引 → (可选)冷路径触发等
        return [
            self._segment_stage,
            self._persist_stage,
            self._sync_stage,
            *self._extra_stages,
        ]

    async def build_context(
        self, input_data: list[RawData]
    ) -> IngestPipelineContext:
        return IngestPipelineContext(raw_data_list=list(input_data))

    async def finalize(self, ctx: IngestPipelineContext) -> list[str]:
        return [c.mem_cell_id for c in ctx.cells]

    def sync_index_repos(
        self,
    ) -> tuple[_ESRepoProto | None, _MilvusRepoProto | None]:
        """返回 SyncIndexStage 绑定的 ES / Milvus 仓储（供 Reconciler 复用）。"""
        return self._sync_stage.es_repo, self._sync_stage.milvus_repo


__all__ = [
    "IngestPipeline",
    "IngestPipelineContext",
    "SegmentStage",
    "PersistMemCellStage",
    "SyncIndexStage",
]
