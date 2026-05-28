"""``/v1/memory/*`` 业务路由。

═══════════════════════════════════════════════════════════════════════════════
已交付端点
═══════════════════════════════════════════════════════════════════════════════

| 方法 | 路径                | 用途                                       |
| ---- | ------------------- | ------------------------------------------ |
| POST | /v1/memory/ingest   | 写入热路径(SBD → MemCell → ES/Milvus 同步) |
| POST | /v1/memory/retrieve | 检索五阶段(召回 → 融合 → 增强 → 过滤 → 重排) |

后续 反馈与生命周期 / 6 会增补 ``/feedback`` / ``/admin/...``。

═══════════════════════════════════════════════════════════════════════════════
契约
═══════════════════════════════════════════════════════════════════════════════
- ingest:请求 :class:`MemoryIngestRequest`,响应 :class:`IngestResponse`
- retrieve:请求 :class:`RetrieveMemRequest`,响应 :class:`RetrieveMemResponse`
- 校验失败 → 422(由 FastAPI / Pydantic 自动)
- 服务未就绪 → 503(``get_*_service`` 抛出)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from memory_app.deps import (
    get_consolidation_service,
    get_ingest_service,
    get_retrieval_orchestrator,
)
from memory_app.format_transfer import ingest_to_raw_data_list
from memory_app.internal_models import RankedMemory
from memory_app.schemas.ingest import MemoryIngestRequest
from memory_app.schemas.retrieve import (
    MemoryHit,
    RetrieveMemRequest,
    RetrieveMemResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/memory", tags=["memory"])


# ════════════════════════════════════════════════════════════════════════════
# 响应模型
# ════════════════════════════════════════════════════════════════════════════
class IngestResponse(BaseModel):
    """``POST /v1/memory/ingest`` 响应。"""

    model_config = ConfigDict(extra="allow")

    #: 落库后的 MemCell 主键列表(顺序与 SBD 切分顺序一致)
    mem_cell_ids: list[str] = Field(default_factory=list)

    #: ``ok`` / ``partial`` / ``empty``;写入热路径 仅 ok / empty
    status: str = "ok"

    #: 总段数(便于客户端做指标埋点)
    segment_count: int = 0


# ════════════════════════════════════════════════════════════════════════════
# 端点
# ════════════════════════════════════════════════════════════════════════════
@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    summary="写入对话/事件,触发 SBD 切分与三库同步",
)
async def ingest_memory(
    request: MemoryIngestRequest,
    service=Depends(get_ingest_service),
) -> IngestResponse:
    """同步热路径:接收 :class:`MemoryIngestRequest` → 经 :func:`ingest_to_raw_data_list`
    转 :class:`RawData` → 委托 :class:`IngestService.ingest`。

    设计要点:
    - 路由层**不**展开管线细节,所有阶段编排见 :class:`IngestPipeline`
    - 路由层**不**直接 import ``plugins_default``;经 ``Depends`` 注入服务
    """
    raw_data_list = ingest_to_raw_data_list(request)
    if not raw_data_list:
        return IngestResponse(mem_cell_ids=[], status="empty", segment_count=0)
    try:
        cell_ids = await service.ingest(raw_data_list)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "ingest failed for tenant=%s user=%s: %s",
            request.tenant_id, request.user_id, e,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"ingest failed: {e.__class__.__name__}",
        )
    return IngestResponse(
        mem_cell_ids=cell_ids,
        status="ok",
        segment_count=len(cell_ids),
    )


# ════════════════════════════════════════════════════════════════════════════
# /retrieve(检索)
# ════════════════════════════════════════════════════════════════════════════
@router.post(
    "/retrieve",
    response_model=RetrieveMemResponse,
    status_code=status.HTTP_200_OK,
    summary="多路召回 → 融合 → 信号增强 → 过滤 → 重排",
)
async def retrieve_memory(
    request: RetrieveMemRequest,
    orchestrator=Depends(get_retrieval_orchestrator),
) -> RetrieveMemResponse:
    """检索五阶段编排。

    设计要点:
    - 路由层只做契约转换,所有阶段编排在
      :class:`memory_app.retrieval.orchestrator.RetrievalOrchestrator`
    - 通道部分失败不阻塞整体响应;全部失败 → 500
    - 空命中 → 200 + ``memories=[]``
    """
    try:
        ranked: list[RankedMemory] = await orchestrator.retrieve(request)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(
            "retrieve failed for tenant=%s user=%s: %s",
            request.tenant_id, request.user_id, e,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"retrieve failed: {e.__class__.__name__}",
        )

    return RetrieveMemResponse(
        query=request.query,
        memories=[_to_memory_hit(m) for m in ranked],
        total=len(ranked),
        debug=_debug_payload(orchestrator, request) if request.debug else None,
    )


def _to_memory_hit(m: RankedMemory) -> MemoryHit:
    return MemoryHit(
        memory_id=m.memory_id,
        content=m.content,
        score=float(m.score),
        memory_type=m.memory_type.value if hasattr(m.memory_type, "value") else str(m.memory_type),
        source_episodes=[],  # 检索 暂不组装证据链
        metadata={
            **(m.metadata or {}),
            "rank": m.rank,
            "source_channel": m.source_channel,
        },
    )


def _debug_payload(orchestrator, request: RetrieveMemRequest) -> dict:
    return {
        "intent": request.intent.value,
        "top_k": request.top_k,
        "enable_graph": request.enable_graph,
    }


# ════════════════════════════════════════════════════════════════════════════
# /consolidate(离线巩固)
# ════════════════════════════════════════════════════════════════════════════
class ConsolidateRequest(BaseModel):
    """``POST /v1/memory/consolidate`` 请求。"""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str | None = None  # None = 对该 tenant 所有 user 扫描
    scope: str = Field(default="all", description="all / light / deep / rem")
    dry_run: bool = False


class ConsolidateResponse(BaseModel):
    """``POST /v1/memory/consolidate`` 响应。"""

    model_config = ConfigDict(extra="allow")

    status: str = "ok"
    phase: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    scanned_count: int = 0
    consolidated_count: int = 0
    archived_count: int = 0
    forgotten_count: int = 0
    error_count: int = 0
    notes: list[str] = Field(default_factory=list)
    detail: dict | None = None


@router.post(
    "/consolidate",
    response_model=ConsolidateResponse,
    status_code=status.HTTP_200_OK,
    summary="离线巩固入口:Sleep + Decay + Capacity",
)
async def consolidate_memory(
    request: ConsolidateRequest,
    service=Depends(get_consolidation_service),
) -> ConsolidateResponse:
    if service is None:
        # ConsolidationService 未装配:返回 200 + ``not_implemented`` 状态
        #
        return ConsolidateResponse(
            status="not_implemented",
            detail={
                "code": "not_implemented",
                "message": "consolidation pipeline not configured",
            },
        )
    try:
        out = await service.consolidate(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            scope=request.scope,
            dry_run=request.dry_run,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(
            "consolidate failed for tenant=%s user=%s: %s",
            request.tenant_id, request.user_id, e,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"consolidate failed: {e.__class__.__name__}",
        )
    return ConsolidateResponse(status="ok", **out)


__all__ = ["router", "IngestResponse", "ConsolidateRequest", "ConsolidateResponse"]
