"""``memory config get|set|history|rollback`` —— /v1/admin/config/*。"""

from __future__ import annotations

import argparse
from typing import ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK, UsageError
from memory_app.cli.output import Emitter, read_json_arg
from memory_app.cli.transport import Transport


class ConfigCommand(Command):
    name: ClassVar[str] = "config"
    help: ClassVar[str] = "/v1/admin/config/*"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        sub = parser.add_subparsers(
            dest="config_action", required=True, metavar="<action>"
        )
        g = sub.add_parser("get", help="解析 category 当前生效配置")
        g.add_argument("category")
        g.add_argument("--tenant-id")
        g.add_argument("--user-id")

        s = sub.add_parser("set", help="写入配置(schema 校验 + 版本自增)")
        s.add_argument("category")
        s.add_argument("--name", required=True)
        s.add_argument("--params", help="JSON 对象(@file 或 inline)")
        s.add_argument("--scope", default="global", choices=["global", "tenant", "user"])
        s.add_argument("--scope-id")
        s.add_argument("--actor", default="cli")
        s.add_argument("--gray-rules", help="JSON 数组(@file 或 inline)")

        h = sub.add_parser("history", help="历史版本(最新优先)")
        h.add_argument("category")
        h.add_argument("--limit", type=int, default=50)

        r = sub.add_parser("rollback", help="回滚到指定历史版本(前进式)")
        r.add_argument("category")
        r.add_argument("--version", type=int, required=True, dest="version")
        r.add_argument("--scope", default="global", choices=["global", "tenant", "user"])
        r.add_argument("--scope-id")
        r.add_argument("--actor", default="cli")

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        action = args.config_action
        if action == "get":
            status, payload = await transport.request(
                "GET", "/v1/admin/config",
                query={
                    "category": args.category,
                    "tenant_id": args.tenant_id,
                    "user_id": args.user_id,
                },
                admin=True,
            )
        elif action == "set":
            params = read_json_arg(args.params) or {}
            if not isinstance(params, dict):
                raise UsageError("--params must be JSON object")
            body: dict = {
                "category": args.category,
                "name": args.name,
                "params": params,
                "scope": args.scope,
                "scope_id": args.scope_id,
                "actor": args.actor,
            }
            if args.gray_rules:
                gr = read_json_arg(args.gray_rules)
                if not isinstance(gr, list):
                    raise UsageError("--gray-rules must be JSON array")
                body["gray_rules"] = gr
            status, payload = await transport.request(
                "POST", "/v1/admin/config", json_body=body, admin=True
            )
        elif action == "history":
            status, payload = await transport.request(
                "GET", "/v1/admin/config/history",
                query={"category": args.category, "limit": args.limit},
                admin=True,
            )
        else:  # rollback
            body = {
                "category": args.category,
                "target_version": args.version,
                "scope": args.scope,
                "scope_id": args.scope_id,
                "actor": args.actor,
            }
            status, payload = await transport.request(
                "POST", "/v1/admin/config/rollback",
                json_body=body, admin=True,
            )
        self.check_2xx(status, payload)
        emitter.emit(payload)
        return EXIT_OK


__all__ = ["ConfigCommand"]
