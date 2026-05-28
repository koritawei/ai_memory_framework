"""LocalTransport —— 进程内调真实 FastAPI app(经 ``httpx.ASGITransport``)。

═══════════════════════════════════════════════════════════════════════════════
为什么不再用 if/elif 大分发
═══════════════════════════════════════════════════════════════════════════════
原 ``cli.py::_local_dispatch`` 380 行 if/elif 复刻了 ``routers/*.py`` 的
全部语义,新增路由必须双写。本类换用 ``httpx.AsyncClient(transport=
ASGITransport(app=memory_app.api:app))`` —— 把请求直接送进 FastAPI 路由,
共享同一鉴权 / Depends / 序列化逻辑;CLI 一行不用改即可跟随路由变更。

═══════════════════════════════════════════════════════════════════════════════
启动 / 关闭
═══════════════════════════════════════════════════════════════════════════════
- 首次 :meth:`request` 调用时 lazy 拉起 ASGI app(走完整 lifespan,与生产一致)
- :meth:`aclose` 优雅停止 lifespan,释放所有外部连接
"""

from __future__ import annotations

import logging
import sys
from contextlib import suppress
from typing import Any

from memory_app.cli.transport.base import Transport

logger = logging.getLogger(__name__)


class LocalTransport(Transport):
    """``--local`` 模式:进程内调 FastAPI app。

    与 :class:`HttpTransport` 行为一致(返回 ``(status, payload)``),让 Command
    handler 一份代码两边复用。
    """

    is_local: bool = True

    def __init__(self, *, admin_key: str | None) -> None:
        self.admin_key = admin_key
        self._client: Any | None = None  # httpx.AsyncClient
        self._lifespan_ctx: Any | None = None  # lifespan async cm

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        # 延迟 import,让 ``memory --help`` 不需要构建 ASGI app
        import httpx

        from memory_app.api import app as fastapi_app

        # FastAPI lifespan 通过 ASGITransport 不会自动跑;手动触发。
        # **顺序关键**:先成功启动 lifespan,再 commit self._client ——
        # 否则 lifespan 启动失败时 self._client 已置,后续 request 会
        # 跳过 init 直接打 ASGI(此时 app_state 尚未装配),导致 503 风暴。
        lifespan_ctx = fastapi_app.router.lifespan_context(fastapi_app)
        try:
            await lifespan_ctx.__aenter__()
        except Exception:
            # 启动失败:必须给 lifespan 生成器一个 __aexit__ 机会,否则
            # 已经进入的 try/finally 块(mongo/es 客户端等)永远不会清理 ——
            # 生成器对象被 GC 时再触发 close 已无法 await,资源泄漏。
            with suppress(Exception):
                await lifespan_ctx.__aexit__(*sys.exc_info())
            # 启动失败:保持字段全 None,让调用方下次重试或冒泡给用户
            self._lifespan_ctx = None
            raise

        self._lifespan_ctx = lifespan_ctx
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fastapi_app),
            base_url="http://memory.local",
            timeout=None,
        )
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        query: dict[str, Any] | None = None,
        admin: bool = False,
    ) -> tuple[int, Any]:
        client = await self._ensure_client()
        headers: dict[str, str] = {"Accept": "application/json"}
        if admin and self.admin_key:
            headers["X-Admin-Key"] = self.admin_key
        params = (
            {k: v for k, v in (query or {}).items() if v is not None}
            if query
            else None
        )
        resp = await client.request(
            method.upper(),
            path,
            json=json_body,
            params=params,
            headers=headers,
        )
        # FastAPI 返回的都是 JSON;非 JSON body 走 raw 包装
        try:
            payload = resp.json()
        except ValueError:
            text = resp.text
            payload = {"raw": text} if text else {}
        return resp.status_code, payload

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:  # noqa: BLE001
                logger.warning("local transport aclose failed: %s", e)
        if self._lifespan_ctx is not None:
            try:
                await self._lifespan_ctx.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.warning("local lifespan shutdown failed: %s", e)
        self._client = None
        self._lifespan_ctx = None


__all__ = ["LocalTransport"]
