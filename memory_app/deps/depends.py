"""FastAPI Depends 工厂(原 ``deps.py`` 内的 ``get_*`` 函数)。

═══════════════════════════════════════════════════════════════════════════════
契约
═══════════════════════════════════════════════════════════════════════════════
- 必启项依赖未装配 → 抛 ``HTTPException(503)`` 让客户端重试
- 可选服务未装配 → 返回 ``None`` —— 路由层据此返回 200 + ``not_implemented``,
  符合"非 500 未定义错误"的设计契约
"""

from __future__ import annotations

from memory_app.deps.state import app_state


def get_ingest_service():
    """从 :data:`app_state` 取已装配的 IngestService;未装配抛 503。"""
    from fastapi import HTTPException, status

    if app_state.ingest_service is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ingest_service not ready",
        )
    return app_state.ingest_service


def get_consolidation_service():
    """取 ConsolidationService(离线巩固);未装配返回 ``None``。"""
    return app_state.consolidation_service


def get_memory_graph():
    """取 MemoryGraph(图与实体);未装配返回 ``None``。"""
    return app_state.memory_graph


def get_mongo_repo():
    """取共享 MongoMemCellRepo(图与实体+);未装配返回 ``None``。"""
    return app_state.mongo_repo


def get_entity_store():
    """取 EntityStore(图与实体);未装配返回 ``None``。"""
    return app_state.entity_store


def get_feedback_service():
    """取 FeedbackService(反馈与生命周期);未装配抛 503。"""
    from fastapi import HTTPException, status

    if app_state.feedback_service is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="feedback_service not ready",
        )
    return app_state.feedback_service


def get_retrieval_orchestrator():
    """取 RetrievalOrchestrator(检索);未装配抛 503。"""
    from fastapi import HTTPException, status

    if app_state.retrieval_orchestrator is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="retrieval_orchestrator not ready",
        )
    return app_state.retrieval_orchestrator


__all__ = [
    "get_ingest_service",
    "get_retrieval_orchestrator",
    "get_feedback_service",
    "get_consolidation_service",
    "get_memory_graph",
    "get_mongo_repo",
    "get_entity_store",
]
