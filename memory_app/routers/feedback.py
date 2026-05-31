"""``/v1/memory/feedback`` 路由(设计文档 §7.5,Phase 5 Step 5.1)。

═══════════════════════════════════════════════════════════════════════════════
契约
═══════════════════════════════════════════════════════════════════════════════
- 请求:`FeedbackRequest`(``schemas/feedback.py``)
- 响应:`FeedbackResponse`(本文件;含 ``old_strength`` / ``new_strength`` /
  ``delta`` / ``access_count`` / ``retrieval_id``)

错误:
- 缺 ``mem_cell_id`` / ``memory_id``    → 422(Pydantic 校验)
- 找不到目标记忆                          → 404
- FeedbackService 未就绪                  → 503
- 强化策略内部异常                        → 500
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from memory_app.deps import get_feedback_service
from memory_app.schemas.feedback import FeedbackRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/memory", tags=["memory"])


class FeedbackResponse(BaseModel):
    """反馈处理结果。"""

    model_config = ConfigDict(extra="allow")

    mem_cell_id: str
    feedback_type: str
    old_strength: float
    new_strength: float
    delta: float
    access_count: int
    retrieval_id: str | None = None
    comment: str | None = None
    status: str = "ok"


@router.post(
    "/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_200_OK,
    summary="显式 / 隐式反馈;触发 Reinforcer 强化策略",
)
async def submit_feedback(
    request: FeedbackRequest,
    service=Depends(get_feedback_service),
) -> FeedbackResponse:
    if not (request.mem_cell_id or request.memory_id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="must provide mem_cell_id or memory_id",
        )
    try:
        result = await service.apply_feedback(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            mem_cell_id=request.mem_cell_id,
            memory_id=request.memory_id,
            feedback_type=request.feedback_type,
            signal_value=request.signal_value,
            comment=request.comment,
            retrieval_id=request.retrieval_id,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(
            "feedback failed for tenant=%s user=%s: %s",
            request.tenant_id, request.user_id, e,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"feedback failed: {e.__class__.__name__}",
        )
    if result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="target memory not found",
        )
    return FeedbackResponse(**result)


__all__ = ["router", "FeedbackResponse"]
