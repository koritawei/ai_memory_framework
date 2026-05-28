"""HttpTransport —— 经标准库 ``urllib`` 调远程 HTTP 服务。

故意不引入 ``httpx``/``requests`` 等运行时依赖 —— HTTP 模式是 CLI 最常用
路径,启动开销与依赖面应最小化。``--local`` 模式才需要 ``httpx``。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from memory_app.cli.errors import UnreachableError
from memory_app.cli.output import safe_json
from memory_app.cli.transport.base import Transport


class HttpTransport(Transport):
    """基于 ``urllib`` 的极简 HTTP 客户端,所有调用以 ``application/json`` 收发。"""

    is_local: bool = False

    def __init__(
        self, server: str, *, admin_key: str | None, timeout: float
    ) -> None:
        self.server = server.rstrip("/")
        self.admin_key = admin_key
        self.timeout = timeout

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        query: dict[str, Any] | None = None,
        admin: bool = False,
    ) -> tuple[int, Any]:
        url = self.server + path
        if query:
            qs = urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}, doseq=True
            )
            if qs:
                url = f"{url}?{qs}"
        data: bytes | None = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if admin and self.admin_key:
            headers["X-Admin-Key"] = self.admin_key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 —— 已通过 server 参数受控
                body = resp.read().decode("utf-8") or "{}"
                return resp.status, safe_json(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            # 空 body 时 e.reason 比 "{}" 更有诊断价值
            payload = safe_json(body) if body else {"detail": str(e.reason)}
            return e.code, payload
        except urllib.error.URLError as e:
            raise UnreachableError(f"server unreachable {url}: {e.reason}") from e
        except TimeoutError as e:
            # Python 3.10+ urlopen 在某些平台直接抛 TimeoutError(不包成 URLError)
            raise UnreachableError(f"server timeout {url}: {e}") from e
        except OSError as e:
            # socket 层失败(连接重置/DNS 错误/SSL 握手失败等)统一归为 unreachable
            raise UnreachableError(f"server unreachable {url}: {e}") from e


__all__ = ["HttpTransport"]
