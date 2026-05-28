"""``memory`` CLI 主入口。

═══════════════════════════════════════════════════════════════════════════════
设计
═══════════════════════════════════════════════════════════════════════════════
- ``main(argv)`` 可被注入 ``argv`` / ``stdout`` / ``stderr``,便于单元测试
- 子命令 handler 异常被 :class:`CliError` 统一捕获 + 退出码映射
- ``--local`` 模式由 :class:`LocalTransport` 在首次请求时 lazy 拉起,关闭由
  ``finally`` 统一释放
"""

from __future__ import annotations

import asyncio
import sys
from typing import TextIO

from memory_app.cli.commands import commands_by_name
from memory_app.cli.errors import EXIT_USAGE, CliError
from memory_app.cli.output import Emitter
from memory_app.cli.parser import build_parser
from memory_app.cli.transport import make_transport


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """CLI 入口。返回退出码,**不**调 ``sys.exit``(便于测试)。"""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse 自身错误 → 退出码 2
        return int(e.code) if isinstance(e.code, int) else EXIT_USAGE

    cmd = commands_by_name().get(args.command)
    if cmd is None:
        print(f"error: unknown command: {args.command}", file=stderr)
        return EXIT_USAGE

    transport = make_transport(args)
    emitter = Emitter(stdout, fmt=args.output)

    try:
        return asyncio.run(_dispatch(cmd, args, transport, emitter))
    except CliError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code


async def _dispatch(cmd, args, transport, emitter) -> int:
    try:
        return await cmd.run(args, transport, emitter)
    finally:
        await transport.aclose()


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
