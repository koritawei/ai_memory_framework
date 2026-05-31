"""``/v1/query/*`` 只读图查询(设计文档 §8 / Phase 7 Step 7.4)。

═══════════════════════════════════════════════════════════════════════════════
端点
═══════════════════════════════════════════════════════════════════════════════
- POST ``/v1/query/user-graph-relations``  返回某实体的相关 mem_cell_id 列表
- POST ``/v1/query/user-memories``         返回用户最近 N 条 MemCell

错误:
- ``enable_graph=false`` / 服务未装配 → 200 + ``status="not_implemented"``
- 校验失败 → 422
- 实体不存在 → 200 + ``related_memories=[]``(非错误)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from memory_app.deps import get_memory_graph, get_mongo_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/query", tags=["query"])


# ════════════════════════════════════════════════════════════════════════════
# user-graph-relations
# ════════════════════════════════════════════════════════════════════════════
class GraphRelationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    entity: str = Field(..., min_length=1)
    max_depth: int = Field(default=2, ge=1, le=3)


class GraphRelationsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "ok"
    entity: str
    related_memories: list[str] = Field(default_factory=list)


@router.post(
    "/user-graph-relations",
    response_model=GraphRelationsResponse,
    status_code=status.HTTP_200_OK,
    summary="返回实体在 MemoryGraph 邻域内的相关记忆",
)
async def user_graph_relations(
    request: GraphRelationsRequest,
    graph=Depends(get_memory_graph),
) -> GraphRelationsResponse:
    if graph is None:
        return GraphRelationsResponse(
            status="not_implemented",
            entity=request.entity,
            related_memories=[],
        )
    try:
        ids = await graph.find_related_memories(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            entity=request.entity,
            max_depth=request.max_depth,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(
            "user_graph_relations failed (tenant=%s user=%s entity=%s): %s",
            request.tenant_id, request.user_id, request.entity, e,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"graph query failed: {e.__class__.__name__}",
        )
    return GraphRelationsResponse(entity=request.entity, related_memories=ids)


# ════════════════════════════════════════════════════════════════════════════
# user-memories
# ════════════════════════════════════════════════════════════════════════════
class UserMemoriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    limit: int = Field(default=20, ge=1, le=200)


class UserMemoryItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    mem_cell_id: str
    text: str
    state: str
    strength: float
    access_count: int
    # ``None`` 表示 created_at 缺失;原先 ``""`` 与 epoch 起点无法区分
    created_at: str | None = None


class UserMemoriesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "ok"
    total: int = 0
    memories: list[UserMemoryItem] = Field(default_factory=list)


@router.post(
    "/user-memories",
    response_model=UserMemoriesResponse,
    status_code=status.HTTP_200_OK,
    summary="返回用户的 MemCell 列表(分页 limit)",
)
async def user_memories(
    request: UserMemoriesRequest,
    repo=Depends(get_mongo_repo),
) -> UserMemoriesResponse:
    if repo is None:
        return UserMemoriesResponse(status="not_implemented")
    try:
        cells = await repo.find_all(request.tenant_id, request.user_id, limit=request.limit)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "user_memories failed (tenant=%s user=%s): %s",
            request.tenant_id, request.user_id, e,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"user_memories failed: {e.__class__.__name__}",
        )
    items = [
        UserMemoryItem(
            mem_cell_id=c.mem_cell_id,
            text=c.text or "",
            state=c.state.value if hasattr(c.state, "value") else str(c.state),
            strength=float(c.strength),
            access_count=int(c.access_count),
            created_at=(
                c.created_at.isoformat()
                if c.created_at is not None else None
            ),
        )
        for c in cells
    ]
    return UserMemoriesResponse(total=len(items), memories=items)


__all__ = [
    "router",
    "GraphRelationsRequest",
    "GraphRelationsResponse",
    "UserMemoriesRequest",
    "UserMemoriesResponse",
]
