"""Command ABC —— 每个 CLI 子命令对应一个独立类。

═══════════════════════════════════════════════════════════════════════════════
设计
═══════════════════════════════════════════════════════════════════════════════
- ``name`` / ``help`` 静态属性 → :func:`build_parser` 据此挂 subparser
- ``configure(parser)`` 子类用 ``add_argument`` 声明自己的 args
- ``run(args, transport, emitter)`` 实际执行;返回退出码
- ``is_async`` 控制 main 是否 ``asyncio.run`` 包装

避免旧版每个 ``cmd_xxx`` handler 都复制 ``transport / try / _check / _emit /
finally aclose`` 模板代码 —— 模板交给 main 统一处理,Command 只关心业务。
"""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from memory_app.cli.errors import BusinessError
from memory_app.cli.output import Emitter
from memory_app.cli.transport import Transport


class Command(ABC):
    """单个 CLI 子命令的契约。"""

    #: 子命令名(``memory health`` 中的 ``health``)
    name: ClassVar[str] = ""

    #: argparse 帮助文本
    help: ClassVar[str] = ""

    #: True 时 main 用 ``asyncio.run`` 包装;False 时直接同步调
    is_async: ClassVar[bool] = True

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """子类用 ``parser.add_argument(...)`` 声明本子命令的参数。"""

    @abstractmethod
    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        """执行本子命令,返回退出码。"""

    # ════════════════════════════════════════════════════════════════════════
    # 公共辅助 —— 子类常用
    # ════════════════════════════════════════════════════════════════════════
    @staticmethod
    def check_2xx(status: int, payload: Any) -> Any:
        """非 2xx 抛 :class:`BusinessError`;原 ``_check`` 的替代。"""
        if 200 <= status < 300:
            return payload
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        import json

        raise BusinessError(
            f"HTTP {status}: {json.dumps(detail, ensure_ascii=False)}"
        )


__all__ = ["Command"]
