"""``memory plugin list|health|reload`` —— /v1/admin/plugins/*。"""

from __future__ import annotations

import argparse
from typing import ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK, UsageError
from memory_app.cli.output import Emitter
from memory_app.cli.transport import Transport


class PluginCommand(Command):
    name: ClassVar[str] = "plugin"
    help: ClassVar[str] = "/v1/admin/plugins/*"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        sub = parser.add_subparsers(
            dest="plugin_action", required=True, metavar="<action>"
        )
        sub.add_parser("list", help="列出注册插件 + 活动实例")
        h = sub.add_parser("health", help="活动实例健康")
        h.add_argument("category", nargs="?")
        h.add_argument("name", nargs="?")
        r = sub.add_parser("reload", help="释放指定插件实例")
        r.add_argument("category")
        r.add_argument("name")
        r.add_argument("--actor", default="cli")

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        action = args.plugin_action
        if action == "list":
            status, payload = await transport.request(
                "GET", "/v1/admin/plugins", admin=True
            )
        elif action == "health":
            if args.category and args.name:
                status, payload = await transport.request(
                    "GET",
                    f"/v1/admin/plugins/{args.category}/{args.name}/health",
                    admin=True,
                )
            else:
                status, payload = await transport.request(
                    "GET", "/v1/admin/plugins/health", admin=True
                )
        else:  # reload
            if not (args.category and args.name):
                raise UsageError("plugin reload requires <category> <name>")
            status, payload = await transport.request(
                "POST",
                f"/v1/admin/plugins/{args.category}/{args.name}/reload",
                query={"actor": args.actor},
                admin=True,
            )
        self.check_2xx(status, payload)
        emitter.emit(payload)
        return EXIT_OK


__all__ = ["PluginCommand"]
