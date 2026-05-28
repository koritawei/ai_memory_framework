"""``memory retrieve`` —— POST /v1/memory/retrieve。"""

from __future__ import annotations

import argparse
from typing import Any, ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK
from memory_app.cli.output import Emitter, read_json_arg
from memory_app.cli.transport import Transport


class RetrieveCommand(Command):
    name: ClassVar[str] = "retrieve"
    help: ClassVar[str] = "POST /v1/memory/retrieve"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--user", required=True)
        parser.add_argument("--query", required=True)
        parser.add_argument("--top-k", type=int, default=10)
        parser.add_argument(
            "--intent", default="auto",
            choices=["auto", "factual", "opinion", "temporal", "multi_hop"],
        )
        parser.add_argument("--enable-graph", action="store_true")
        parser.add_argument("--filters", help="JSON 过滤条件(@file 或 inline)")
        parser.add_argument("--debug", action="store_true")

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        body: dict[str, Any] = {
            "tenant_id": args.tenant,
            "user_id": args.user,
            "query": args.query,
            "top_k": args.top_k,
            "intent": args.intent,
            "enable_graph": args.enable_graph,
        }
        if args.filters:
            body["filters"] = read_json_arg(args.filters)
        if args.debug:
            body["debug"] = True
        status, payload = await transport.request(
            "POST", "/v1/memory/retrieve", json_body=body
        )
        self.check_2xx(status, payload)
        emitter.emit(payload)
        return EXIT_OK


__all__ = ["RetrieveCommand"]
