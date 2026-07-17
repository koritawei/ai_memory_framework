"""HTTP 限流中间件 —— 基于 limits 库（Moving Window）。"""

from __future__ import annotations

import logging
from typing import Any

from limits import parse
from limits.aio.strategies import MovingWindowRateLimiter
from starlette.requests import Request
from starlette.responses import JSONResponse

from memory_app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _build_limiter(settings: Settings) -> tuple[MovingWindowRateLimiter, Any]:
    """按配置构造 limits 策略与 RateLimitItem。"""
    item = parse(f"{max(1, int(settings.rate_limit_rpm))}/minute")
    if settings.rate_limit_backend == "redis":
        try:
            from limits.aio.storage.redis import RedisStorage

            storage = RedisStorage(
                settings.redis_url,
                implementation="redispy",
                key_prefix="memory:rl",
            )
            return MovingWindowRateLimiter(storage), item
        except Exception as e:  # noqa: BLE001
            logger.warning("limits RedisStorage init failed, fallback memory: %s", e)
    from limits.aio.storage.memory import MemoryStorage

    return MovingWindowRateLimiter(MemoryStorage()), item


class RateLimitMiddleware:
    """按已验证身份或 client IP 做 RPM 限流（limits Moving Window）。"""

    _SKIP_PREFIXES = (
        "/health/",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/v1/admin/",
    )

    def __init__(self, app, settings: Settings | None = None):
        self.app = app
        self._settings = settings
        self._limiter: MovingWindowRateLimiter | None = None
        self._item: Any | None = None
        self._limiter_backend: str | None = None

    def _settings_or_default(self) -> Settings:
        return self._settings or get_settings()

    def _ensure_limiter(self, settings: Settings) -> tuple[MovingWindowRateLimiter, Any]:
        backend = settings.rate_limit_backend
        if (
            self._limiter is None
            or self._item is None
            or self._limiter_backend != backend
        ):
            self._limiter, self._item = _build_limiter(settings)
            self._limiter_backend = backend
        return self._limiter, self._item

    def _rate_key(self, request: Request) -> str:
        identity = getattr(request.state, "identity", None)
        if identity is not None:
            if identity.user_id:
                return f"user:{identity.tenant_id}:{identity.user_id}"
            return f"tenant:{identity.tenant_id}"
        client = request.client.host if request.client else "unknown"
        return f"ip:{client}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        settings = self._settings_or_default()
        path = request.url.path
        if not settings.rate_limit_enabled or any(
            path.startswith(p) for p in self._SKIP_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        limiter, item = self._ensure_limiter(settings)
        key = self._rate_key(request)
        try:
            allowed = await limiter.hit(item, key)
        except Exception as e:  # noqa: BLE001
            logger.warning("rate limit check failed (allow): %s", e)
            allowed = True
        if not allowed:
            response = JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


__all__ = ["RateLimitMiddleware"]
