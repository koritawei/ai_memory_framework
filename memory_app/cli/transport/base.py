"""Transport ABC —— HTTP / Local 模式统一契约。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
- :class:`HttpTransport` 经标准库 ``urllib`` 调远程 HTTP 服务
- :class:`LocalTransport` 经 ``httpx.ASGITransport`` 在进程内调 FastAPI app
  — 路由语义不再 CLI 内重复实现

两者共享 ``async def request(method, path, *, json_body, query, admin) ->
(status, payload)`` 契约,让 Command handler 写一份代码两边复用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Transport(ABC):
    """HTTP / Local 模式共同契约。"""

    is_local: bool = False

    @abstractmethod
    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        query: dict[str, Any] | None = None,
        admin: bool = False,
    ) -> tuple[int, Any]:
        """发送一次请求;返回 ``(http_status, payload_dict_or_str)``。

        - ``json_body``:若非 None 则 POST/PUT 时塞 body(``application/json``)
        - ``query``:URL query 参数,值为 None 的键被过滤
        - ``admin``:为 True 时注入 ``X-Admin-Key`` 头(从构造参数取)
        - 非 admin 且配置了 ``api_key`` 时注入 ``Authorization: Bearer``
        """

    async def aclose(self) -> None:
        """优雅关闭。默认 no-op;持有连接 / 客户端的子类覆盖。"""
        return None


__all__ = ["Transport"]
