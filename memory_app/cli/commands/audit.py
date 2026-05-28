"""``memory audit`` —— 业务平面零硬依赖审计(``scripts/audit_no_hard_deps``)。

═══════════════════════════════════════════════════════════════════════════════
路径处理(预存在问题的兜底)
═══════════════════════════════════════════════════════════════════════════════
``scripts/`` 不是 Python 包(无 ``__init__.py``),也不在 ``pyproject.toml``
``[tool.setuptools.packages.find].include`` 内。直接 ``import scripts.xxx``
仅当 cwd 包含 ``scripts/`` 目录时才生效(如 pytest 启动)。

为支持 ``memory audit`` 从任意工作目录跑,本类在 import 前主动把项目根加入
``sys.path``;并在仍失败时给出明确指引。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK, BusinessError
from memory_app.cli.output import Emitter
from memory_app.cli.transport import Transport


class AuditCommand(Command):
    name: ClassVar[str] = "audit"
    help: ClassVar[str] = "业务平面零硬依赖审计(脚本壳)"

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        # 推算项目根:本文件位于 ``<project>/memory_app/cli/commands/audit.py``
        project_root = Path(__file__).resolve().parents[3]
        scripts_dir = project_root / "scripts"
        if scripts_dir.is_dir() and str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        try:
            from scripts.audit_no_hard_deps import main as audit_main
        except ImportError as e:
            raise BusinessError(
                f"unable to import scripts.audit_no_hard_deps "
                f"(expected at {scripts_dir}): {e}; "
                "请在项目根目录运行 ``memory audit``"
            ) from e

        return int(audit_main(argv=[]) or EXIT_OK)


__all__ = ["AuditCommand"]
