"""RateLimitMiddleware（limits）冒烟测试。"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from memory_app.middleware.rate_limit import RateLimitMiddleware
from memory_app.settings import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        app_name="t",
        debug=True,
        auth_enabled=False,
        admin_api_key=None,
        api_key=None,
        config_center_backend="file",
        config_dir="config",
        mongo_uri="mongodb://localhost",
        mongo_db="t",
        redis_url="redis://localhost:6379/0",
        es_url="http://localhost:9200",
        es_index_prefix="t",
        milvus_host="localhost",
        milvus_port=19530,
        milvus_collection="t",
        dlq_backend="memory",
        task_runner_backend="asyncio",
        task_queue_key="t",
        task_runner_consumer_enabled=False,
        background_max_concurrent=8,
        rate_limit_enabled=True,
        rate_limit_rpm=2,
        rate_limit_backend="memory",
        dlq_reconcile_interval_s=0,
        dlq_reconcile_batch_size=10,
        dlq_reconcile_max_retries=5,
        cold_path_llm_max_concurrent=2,
        sync_index_max_concurrent=4,
        health_require_deps=False,
    )
    base.update(overrides)
    # Settings may require more fields — use model_construct if validation fails
    try:
        return Settings(**base)  # type: ignore[arg-type]
    except Exception:
        return Settings.model_construct(**base)


@pytest.fixture
def limited_app(monkeypatch, tmp_path):
    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/v1/memory/ingest", ok, methods=["POST"])])
    settings = _settings()
    app.add_middleware(RateLimitMiddleware, settings=settings)
    return app


def test_rate_limit_returns_429_after_rpm(limited_app):
    client = TestClient(limited_app)
    assert client.post("/v1/memory/ingest").status_code == 200
    assert client.post("/v1/memory/ingest").status_code == 200
    assert client.post("/v1/memory/ingest").status_code == 429
