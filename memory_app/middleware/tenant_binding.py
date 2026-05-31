"""租户绑定校验中间件。"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from memory_app.deps import app_state
from memory_app.security.identity import validate_body_tenant
from memory_app.settings import get_settings

logger = logging.getLogger(__name__)


class TenantBindingMiddleware:
    """当 ``tenant_binding_enabled`` 时校验 JSON body 与 ``request.state.identity`` 一致。"""

    _CHECK_PREFIXES = ("/v1/memory/", "/v1/query/")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        settings = app_state.settings or get_settings()
        path = request.url.path
        if (
            not settings.tenant_binding_enabled
            or request.method != "POST"
            or not any(path.startswith(p) for p in self._CHECK_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        identity = getattr(request.state, "identity", None)
        if identity is None:
            if settings.auth_enabled:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "tenant binding requires authenticated identity"},
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        body = await request.body()

        async def receive_replay():
            return {"type": "http.request", "body": body, "more_body": False}

        replay_request = Request(scope, receive_replay)
        try:
            payload = json.loads(body.decode("utf-8") if body else "{}")
        except json.JSONDecodeError:
            response = JSONResponse(
                status_code=400, content={"detail": "invalid JSON body"}
            )
            await response(scope, receive, send)
            return
        body_tenant = payload.get("tenant_id")
        body_user = payload.get("user_id")
        if not body_tenant:
            await self.app(scope, receive_replay, send)
            return
        try:
            validate_body_tenant(identity, str(body_tenant), body_user)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                response = JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )
                await response(scope, receive, send)
                return
            raise
        if hasattr(request.state, "request_id"):
            replay_request.state.request_id = request.state.request_id
        replay_request.state.identity = identity
        await self.app(scope, receive_replay, send)


__all__ = ["TenantBindingMiddleware"]
