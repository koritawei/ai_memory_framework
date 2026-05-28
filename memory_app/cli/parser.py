"""argparse 装配 —— 遍历 :data:`COMMANDS` 自动挂 subparser。

═══════════════════════════════════════════════════════════════════════════════
设计
═══════════════════════════════════════════════════════════════════════════════
- 根 parser 挂"通用 transport / 输出"选项(--server / --local / --admin-key /
  --timeout / --output)
- 每个 :class:`Command` 自治声明自己的 ``configure(subparser)``
- 子命令 → 处理函数的映射:解析后由 :func:`memory_app.cli.main.main` 据
  ``args.command`` 从 :func:`commands_by_name` 查
"""

from __future__ import annotations

import argparse
import os

from memory_app.cli.commands import COMMANDS

DEFAULT_SERVER = "http://127.0.0.1:8000"
ENV_SERVER = "MEMORY_CLI_SERVER"
ENV_ADMIN_KEY = "MEMORY_ADMIN_KEY"


def _add_common(parser: argparse.ArgumentParser) -> None:
    """根 parser 通用选项。挂根而非每个 subparser,避免 ``memory config set X
    --server Y`` 这类"位置依赖"歧义。"""
    g = parser.add_argument_group("transport")
    g.add_argument(
        "--server",
        default=os.environ.get(ENV_SERVER, DEFAULT_SERVER),
        help=f"HTTP 目标(默认 ${ENV_SERVER} 或 {DEFAULT_SERVER})",
    )
    g.add_argument("--local", action="store_true", help="进程内直连模式(评测 / 烟测)")
    g.add_argument(
        "--admin-key",
        default=os.environ.get(ENV_ADMIN_KEY),
        help=f"管理面 X-Admin-Key(默认 ${ENV_ADMIN_KEY})",
    )
    g.add_argument(
        "--timeout", type=float, default=30.0,
        help="HTTP 超时秒(默认 30)",
    )
    g.add_argument(
        "--output", choices=["pretty", "json", "raw"], default="pretty",
        help="输出格式:pretty=缩进 JSON / json=单行 JSON / raw=原文",
    )


def build_parser() -> argparse.ArgumentParser:
    """构建 argparse 根 parser + 全部子命令。"""
    p = argparse.ArgumentParser(
        prog="memory",
        description=(
            "Memory Service CLI —— 分层认知记忆系统统一入口"
        ),
    )
    _add_common(p)
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")
    for cmd in COMMANDS:
        sp = sub.add_parser(cmd.name, help=cmd.help)
        cmd.configure(sp)
    return p


__all__ = [
    "build_parser",
    "DEFAULT_SERVER",
    "ENV_SERVER",
    "ENV_ADMIN_KEY",
]
