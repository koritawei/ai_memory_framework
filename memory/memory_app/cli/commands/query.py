"""``memory query graph|memories`` —— POST /v1/query/*。"""

from __future__ import annotations

import argparse
from typing import ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK
from memory_app.cli.output import Emitter
from memory_app.cli.transport import Transport


class QueryCommand(Command):
    name: ClassVar[str] = "query"
    help: ClassVar[str] = "POST /v1/query/*"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        sub = parser.add_subparsers(
            dest="query_action", required=True, metavar="<action>"
        )
        g = sub.add_parser("graph", help="user-graph-relations")
        g.add_argument("--tenant", required=True)
        g.add_argument("--user", required=True)
        g.add_argument("--entity", required=True)
        g.add_argument("--depth", type=int, default=2)

        m = sub.add_parser("memories", help="user-memories")
        m.add_argument("--tenant", required=True)
        m.add_argument("--user", required=True)
        m.add_argument("--limit", type=int, default=20)

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        if args.query_action == "graph":
            body = {
                "tenant_id": args.tenant,
                "user_id": args.user,
                "entity": args.entity,
                "max_depth": args.depth,
            }
            path = "/v1/query/user-graph-relations"
        else:
            body = {
                "tenant_id": args.tenant,
                "user_id": args.user,
                "limit": args.limit,
            }
            path = "/v1/query/user-memories"
        status, payload = await transport.request("POST", path, json_body=body)
        self.check_2xx(status, payload)
        emitter.emit(payload)
        return EXIT_OK


__all__ = ["QueryCommand"]
