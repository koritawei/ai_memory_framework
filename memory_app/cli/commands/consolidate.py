"""``memory consolidate`` —— POST /v1/memory/consolidate。"""

from __future__ import annotations

import argparse
from typing import ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK
from memory_app.cli.output import Emitter
from memory_app.cli.transport import Transport


class ConsolidateCommand(Command):
    name: ClassVar[str] = "consolidate"
    help: ClassVar[str] = "POST /v1/memory/consolidate"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--user")
        parser.add_argument(
            "--scope", default="all",
            choices=["all", "light", "deep", "rem"],
        )
        parser.add_argument("--dry-run", action="store_true")

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        body = {
            "tenant_id": args.tenant,
            "user_id": args.user,
            "scope": args.scope,
            "dry_run": args.dry_run,
        }
        status, payload = await transport.request(
            "POST", "/v1/memory/consolidate", json_body=body
        )
        self.check_2xx(status, payload)
        emitter.emit(payload)
        return EXIT_OK


__all__ = ["ConsolidateCommand"]
