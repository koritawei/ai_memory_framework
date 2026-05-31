"""``memory serve`` —— 拉起 uvicorn 托管 :mod:`memory_app.api`。"""

from __future__ import annotations

import argparse
from typing import ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK, BusinessError
from memory_app.cli.output import Emitter
from memory_app.cli.transport import Transport


class ServeCommand(Command):
    name: ClassVar[str] = "serve"
    help: ClassVar[str] = "启动 uvicorn (memory_app.api:app)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8000)
        parser.add_argument("--reload", action="store_true")
        parser.add_argument(
            "--log-level", default="info",
            choices=["critical", "error", "warning", "info", "debug", "trace"],
        )

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        try:
            import uvicorn
        except ImportError as e:
            raise BusinessError(
                "uvicorn not installed; run `uv sync` or "
                "`pip install 'uvicorn[standard]'`"
            ) from e
        # ``--reload`` 必须经 ``uvicorn.run`` —— 它 fork 子进程做文件 watcher,
        # 不能在外层 asyncio loop 里;改 spawn subprocess。
        if args.reload:
            import subprocess
            import sys

            cmd = [
                sys.executable, "-m", "uvicorn", "memory_app.api:app",
                "--host", args.host, "--port", str(args.port),
                "--log-level", args.log_level, "--reload",
            ]
            return subprocess.call(cmd)
        # 常规模式:在当前事件循环里跑 uvicorn.Server.serve()(async)
        config = uvicorn.Config(
            "memory_app.api:app",
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
        server = uvicorn.Server(config)
        await server.serve()
        return EXIT_OK


__all__ = ["ServeCommand"]
