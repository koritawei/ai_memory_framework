"""FastAPI 应用入口（设计文档 §2.5）。

横切能力（Phase 8）：
- 鉴权：Bearer / X-Admin-Key
- 租户绑定：JWT / API Key 映射 / 网关头
- 可观测：X-Request-Id + Prometheus ``/metrics``
- 限流：按 IP / X-User-Id 令牌桶
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from memory_app import __version__
from memory_app.deps import app_state
from memory_app.middleware.metrics import MetricsMiddleware, metrics_response
from memory_app.middleware.rate_limit import RateLimitMiddleware
from memory_app.middleware.tenant_binding import TenantBindingMiddleware
from memory_app.prompt_runtime import init_prompt_manager
from memory_app.routers import admin as admin_router
from memory_app.routers import feedback as feedback_router
from memory_app.routers import health as health_router
from memory_app.routers import memory as memory_router
from memory_app.routers import query as query_router
from memory_app.security.auth import check_api_key, verify_secret
from memory_app.security.identity import identity_from_gateway_headers, resolve_identity
from memory_app.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("memory_app.api")


class _RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


class _BusinessAuthMiddleware(BaseHTTPMiddleware):
    _SKIP_PREFIXES = ("/health/", "/v1/admin/", "/docs", "/openapi.json", "/redoc", "/metrics")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self._SKIP_PREFIXES):
            return await call_next(request)
        settings = app_state.settings or get_settings()
        if not settings.auth_enabled:
            return await call_next(request)
        auth_header = request.headers.get("Authorization")
        scheme, _, token = (auth_header or "").partition(" ")
        from fastapi.security import HTTPAuthorizationCredentials

        credentials = (
            HTTPAuthorizationCredentials(scheme=scheme, credentials=token)
            if scheme and token
            else None
        )
        try:
            check_api_key(credentials)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=getattr(exc, "headers", None) or {},
                )
            raise
        identity = resolve_identity(settings, credentials)
        if identity is None and settings.trust_gateway_headers:
            global_key = (
                credentials is not None
                and settings.api_key
                and verify_secret(credentials.credentials, settings.api_key)
            )
            if not global_key:
                identity = identity_from_gateway_headers(request.headers)
        if identity is not None:
            request.state.identity = identity
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    from memory_app.security.startup import validate_startup_security

    validate_startup_security(settings)
    logger.info(
        "starting %s v%s (debug=%s, config_backend=%s, dlq=%s, tasks=%s)",
        settings.app_name,
        __version__,
        settings.debug,
        settings.config_center_backend,
        settings.dlq_backend,
        settings.task_runner_backend,
    )
    await app_state.init(settings)
    try:
        await init_prompt_manager(app_state.config_center)
    except Exception as e:  # noqa: BLE001
        logger.warning("prompt manager init failed: %s", e)

    reconcile_task: asyncio.Task | None = None
    reconcile_lock = asyncio.Lock()
    interval = settings.dlq_reconcile_interval_s
    if interval > 0:

        async def _auto_reconcile_loop() -> None:
            from memory_app.concurrency import RedisDistributedLock
            from memory_app.reconciliation.sync_reconciler import build_reconciler_from_state

            lock_key = f"{settings.task_queue_key}:dlq_reconcile_lock"
            while True:
                await asyncio.sleep(interval)
                if reconcile_lock.locked():
                    logger.debug("skip dlq reconcile: previous run still active")
                    continue
                async with reconcile_lock:
                    dist_lock: RedisDistributedLock | None = None
                    redis = getattr(app_state.clients, "redis_client", None)
                    if redis is not None:
                        dist_lock = RedisDistributedLock(
                            redis,
                            lock_key,
                            ttl_s=max(60, interval * 2),
                        )
                        if not await dist_lock.acquire():
                            logger.debug("skip dlq reconcile: another replica holds lock")
                            continue
                    try:
                        rec = build_reconciler_from_state(app_state)
                        if rec is None:
                            continue
                        try:
                            await rec.reconcile(limit=settings.dlq_reconcile_batch_size)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("auto dlq reconcile failed: %s", e)
                    finally:
                        if dist_lock is not None:
                            await dist_lock.release()

        reconcile_task = asyncio.create_task(_auto_reconcile_loop())

    try:
        yield
    finally:
        if reconcile_task is not None:
            reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconcile_task
        logger.info("shutting down %s", settings.app_name)
        await app_state.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="AI Memory 系统 —— 分层认知记忆架构（Phase 0）",
        lifespan=lifespan,
    )
    # 后添加 = 更外层；Metrics 最外以统计 401/429
    # 入站顺序(外→内): Metrics → RequestId → BusinessAuth → RateLimit → TenantBinding → 路由
    app.add_middleware(TenantBindingMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(_BusinessAuthMiddleware)
    app.add_middleware(_RequestIdMiddleware)
    if settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware)

    if settings.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics():
            return metrics_response()

    app.include_router(health_router.router)
    app.include_router(admin_router.router)
    app.include_router(memory_router.router)
    app.include_router(feedback_router.router)
    app.include_router(query_router.router)
    return app


app = create_app()
