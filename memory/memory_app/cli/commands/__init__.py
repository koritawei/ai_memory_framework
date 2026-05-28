"""Commands 注册表 —— 所有 CLI 子命令汇总。

新增子命令:
1. 在本目录新建 ``my_command.py``,继承 :class:`Command`
2. 把类追加进下面 ``COMMANDS`` 列表
3. parser.py 会自动挂载 subparser
"""

from __future__ import annotations

from memory_app.cli.commands.audit import AuditCommand
from memory_app.cli.commands.base import Command
from memory_app.cli.commands.config import ConfigCommand
from memory_app.cli.commands.consolidate import ConsolidateCommand
from memory_app.cli.commands.feedback import FeedbackCommand
from memory_app.cli.commands.health import HealthCommand
from memory_app.cli.commands.ingest import IngestCommand
from memory_app.cli.commands.plugin import PluginCommand
from memory_app.cli.commands.prompt import PromptCommand
from memory_app.cli.commands.query import QueryCommand
from memory_app.cli.commands.retrieve import RetrieveCommand
from memory_app.cli.commands.serve import ServeCommand

#: 所有子命令实例(顺序仅影响 --help 输出)
COMMANDS: list[Command] = [
    ServeCommand(),
    HealthCommand(),
    IngestCommand(),
    RetrieveCommand(),
    FeedbackCommand(),
    ConsolidateCommand(),
    QueryCommand(),
    PluginCommand(),
    ConfigCommand(),
    PromptCommand(),
    AuditCommand(),
]


def commands_by_name() -> dict[str, Command]:
    """``{name: command}`` 索引,供 main 按 args.command 查询。"""
    return {c.name: c for c in COMMANDS}


__all__ = ["Command", "COMMANDS", "commands_by_name"]
