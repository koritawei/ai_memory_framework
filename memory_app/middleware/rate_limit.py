"""令牌桶限流中间件。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from memory_app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class _MemoryBucket:
    def __init__(self, rpm: int) -> None:
        self._capacity = max(1, rpm)
        self._tokens = float(self._capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def allow(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            refill = (elapsed / 60.0) * self._capacity
            self._tokens = min(self._capacity, self._tokens + refill)
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


class RateLimitMiddleware:
    """按已验证身份或 client IP 做 RPM 限流。"""

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
        self._buckets: dict[str, _MemoryBucket] = {}

    def _settings_or_default(self) -> Settings:
        return self._settings or get_settings()

    def _rate_key(self, request: Request) -> str:
        identity = getattr(request.state, "identity", None)
        if identity is not None:
            if identity.user_id:
                return f"user:{identity.tenant_id}:{identity.user_id}"
            return f"tenant:{identity.tenant_id}"
        client = request.client.host if request.client else "unknown"
        return f"ip:{client}"

    async def _allow_redis(self, redis: Any, key: str, rpm: int) -> bool:
        window_key = f"memory:rl:{key}:{int(time.time()) // 60}"
        try:
            count = await redis.incr(window_key)
            if count == 1:
                await redis.expire(window_key, 120)
            return int(count) <= rpm
        except Exception as e:  # noqa: BLE001
            logger.warning("redis rate limit failed (memory fallback): %s", e)
            bucket = self._buckets.setdefault(key, _MemoryBucket(rpm))
            return await bucket.allow()

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
        rpm = max(1, int(settings.rate_limit_rpm))
        key = self._rate_key(request)
        allowed = True
        if settings.rate_limit_backend == "redis":
            from memory_app.deps import app_state

            redis = app_state.clients.redis_client if app_state else None
            if redis is not None:
                allowed = await self._allow_redis(redis, key, rpm)
            else:
                bucket = self._buckets.setdefault(key, _MemoryBucket(rpm))
                allowed = await bucket.allow()
        else:
            bucket = self._buckets.setdefault(key, _MemoryBucket(rpm))
            allowed = await bucket.allow()
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
