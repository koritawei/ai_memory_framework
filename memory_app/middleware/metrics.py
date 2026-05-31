"""Prometheus 指标采集。"""

from __future__ import annotations

import time

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.requests import Request
from starlette.responses import Response

HTTP_REQUESTS = Counter(
    "memory_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
HTTP_LATENCY = Histogram(
    "memory_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
BACKGROUND_SUBMITTED = Counter(
    "memory_background_tasks_submitted_total",
    "Background tasks submitted",
    ["backend"],
)
BACKGROUND_FAILED = Counter(
    "memory_background_tasks_failed_total",
    "Background tasks failed to DLQ",
    ["backend"],
)
DLQ_SIZE = Gauge("memory_dlq_records", "Current DLQ backlog size")
DLQ_RECONCILE_SUCCEEDED = Counter(
    "memory_dlq_reconcile_success_total",
    "DLQ records successfully reconciled",
    ["target"],
)
DLQ_RECONCILE_FAILED = Counter(
    "memory_dlq_reconcile_failure_total",
    "DLQ reconcile attempts failed",
    ["target"],
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def observe_request(method: str, path: str, status_code: int, duration_s: float) -> None:
    route = _normalize_path(path)
    HTTP_REQUESTS.labels(method=method, path=route, status=str(status_code)).inc()
    HTTP_LATENCY.labels(method=method, path=route).observe(duration_s)


def _normalize_path(path: str) -> str:
    """聚合动态段，避免高基数 label。"""
    if path.startswith("/v1/admin/plugins/") and path.endswith("/health"):
        return "/v1/admin/plugins/{category}/{name}/health"
    if path.startswith("/v1/admin/plugins/") and path.endswith("/reload"):
        return "/v1/admin/plugins/{category}/{name}/reload"
    if path.startswith("/v1/admin/prompts/"):
        return "/v1/admin/prompts/{id}"
    return path


class MetricsMiddleware:
    """记录 HTTP 请求计数与延迟。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.url.path == "/metrics":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)
        duration = time.perf_counter() - start
        observe_request(request.method, request.url.path, status_code, duration)


__all__ = [
    "MetricsMiddleware",
    "metrics_response",
    "observe_request",
    "BACKGROUND_SUBMITTED",
    "BACKGROUND_FAILED",
    "CONTENT_TYPE_LATEST",
]
