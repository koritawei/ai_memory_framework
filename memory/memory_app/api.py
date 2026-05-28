"""FastAPI 应用入口。

职责：
1. 定义 ``lifespan`` 上下文管理器：在应用启动 / 关闭时统一拉起 / 释放
   :class:`memory_app.deps.AppState`（DB 连接池、ConfigCenter、PluginFactory 等）。
2. 通过 :func:`create_app` 工厂函数装配 FastAPI 实例 + 挂载路由。
3. 暴露模块级 ``app`` 单例，供 ``uvicorn memory_app.api:app`` 直接启动。

启动方式：

.. code-block:: bash

    # 默认走 config/bootstrap.yaml + config/default.yaml
    uv run uvicorn memory_app.api:app --host 127.0.0.1 --port 8000

    # 重定向 bootstrap.yaml 路径（K8s / Docker 场景）
    MEMORY_BOOTSTRAP_FILE=/etc/memory/bootstrap.prod.yaml \\
        uv run uvicorn memory_app.api:app --host 0.0.0.0 --port 8000

横切能力（按  计划逐步加入）：
- 鉴权：``Authorization: Bearer`` / ``X-Admin-Key`` —— 已在 admin 路由生效
- 可观测：``X-Request-Id`` / Prometheus metrics —— 管理面 落地
- 限流：按 ``user_id`` 或 IP 的令牌桶 —— 管理面 落地
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from memory_app import __version__
from memory_app.deps import app_state
from memory_app.prompt_runtime import init_prompt_manager
from memory_app.routers import admin as admin_router
from memory_app.routers import feedback as feedback_router
from memory_app.routers import health as health_router
from memory_app.routers import memory as memory_router
from memory_app.routers import query as query_router
from memory_app.settings import get_settings

# 全局日志格式：固定为 ISO 风格，便于 grep / Loki 解析
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("memory_app.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan：启动时 ``init`` 全部依赖，关闭时优雅 ``close``。

    任意外部依赖（Mongo/ES/Redis/Milvus）不可达**不会**阻塞启动 —— 仅在
    ``/health/ready`` 上报 degraded（与  服务降级策略一致）。
    """
    settings = get_settings()
    logger.info(
        "starting %s v%s (debug=%s, config_backend=%s)",
        settings.app_name,
        __version__,
        settings.debug,
        settings.config_center_backend,
    )
    await app_state.init(settings)
    # ConfigCenter 已就绪 → 绑定 Prompt 单例(脚手架 );
    # 失败仅 warn,使 冷路径 提取器仍能回退到 StandalonePromptManager
    try:
        await init_prompt_manager(app_state.config_center)
    except Exception as e:  # noqa: BLE001
        logger.warning("prompt manager init failed: %s", e)
    try:
        yield
    finally:
        logger.info("shutting down %s", settings.app_name)
        await app_state.close()


def create_app() -> FastAPI:
    """工厂函数：装配 FastAPI 实例。便于测试以独立 ``Settings`` 启动多个实例。"""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,            # 来自 bootstrap.yaml，env 可覆盖
        version=__version__,
        description="AI Memory 系统 —— 分层认知记忆架构（脚手架）",
        lifespan=lifespan,
    )
    # 路由按"先核心、后扩展"顺序挂载；新 router 在此一行追加即可
    app.include_router(health_router.router)
    app.include_router(admin_router.router)
    app.include_router(memory_router.router)  # 写入热路径/4: /v1/memory/ingest, /retrieve
    app.include_router(feedback_router.router)  # 反馈与生命周期: /v1/memory/feedback
    app.include_router(query_router.router)  # 图与实体: /v1/query/*
    return app


# 模块级单例：``uvicorn memory_app.api:app`` 直接消费
app = create_app()
