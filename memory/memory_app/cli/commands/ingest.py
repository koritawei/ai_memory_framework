"""``memory ingest`` —— POST /v1/memory/ingest。

两种入参模式:
- ``--payload @file.json`` 提供完整 JSON 请求体
- ``--tenant / --user / --session-id / --turn`` 多次构造最小载荷
"""

from __future__ import annotations

import argparse
from typing import Any, ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK, UsageError
from memory_app.cli.output import Emitter, read_json_arg
from memory_app.cli.transport import Transport


class IngestCommand(Command):
    name: ClassVar[str] = "ingest"
    help: ClassVar[str] = "POST /v1/memory/ingest"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--tenant", help="tenant_id(与 --payload 互斥)")
        parser.add_argument("--user", help="user_id")
        parser.add_argument("--session-id", help="session_id(默认 cli-session)")
        parser.add_argument(
            "--turn", action="append", default=[],
            help="形如 'user:你好';可重复",
        )
        parser.add_argument("--payload", help="完整 JSON 入参(@file 或 inline)")

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        body = self._build_body(args)
        status, payload = await transport.request(
            "POST", "/v1/memory/ingest", json_body=body
        )
        self.check_2xx(status, payload)
        emitter.emit(payload)
        return EXIT_OK

    @staticmethod
    def _build_body(args: argparse.Namespace) -> dict[str, Any]:
        if args.payload:
            body = read_json_arg(args.payload)
            if not isinstance(body, dict):
                raise UsageError("payload must be JSON object")
            return body
        if not args.tenant or not args.user:
            raise UsageError("--tenant and --user required when --payload omitted")
        if not args.turn:
            raise UsageError("at least one --turn role:content required")
        turns: list[dict[str, str]] = []
        for raw in args.turn:
            if ":" not in raw:
                raise UsageError(f"invalid --turn '{raw}', expect role:content")
            role, content = raw.split(":", 1)
            turns.append({"role": role.strip(), "content": content.strip()})
        return {
            "tenant_id": args.tenant,
            "user_id": args.user,
            "session_id": args.session_id,
            "history_sessions": [
                {
                    "session_id": args.session_id or "cli-session",
                    "turns": turns,
                },
            ],
        }


__all__ = ["IngestCommand"]
