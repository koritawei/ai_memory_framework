"""``memory_app.cli`` —— CLI 包入口。

═══════════════════════════════════════════════════════════════════════════════
背向兼容
═══════════════════════════════════════════════════════════════════════════════
原 ``memory_app/cli.py`` 单文件(1192 行)拆为本包:

- :func:`main`                     CLI 入口(从 :mod:`.main`)
- :func:`build_parser`             argparse 装配
- :class:`Command` / :data:`COMMANDS` 子命令注册表

``pyproject.toml`` 入口点保持 ``memory = "memory_app.cli:main"`` —— 因本模块
re-export ``main``,无需修改 entry point。
"""

from __future__ import annotations

from memory_app.cli.commands import COMMANDS, Command
from memory_app.cli.main import main
from memory_app.cli.parser import build_parser

__all__ = ["main", "build_parser", "Command", "COMMANDS"]
