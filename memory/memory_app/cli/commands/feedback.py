"""``memory feedback`` —— POST /v1/memory/feedback。"""

from __future__ import annotations

import argparse
from typing import ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK, UsageError
from memory_app.cli.output import Emitter
from memory_app.cli.transport import Transport


class FeedbackCommand(Command):
    name: ClassVar[str] = "feedback"
    help: ClassVar[str] = "POST /v1/memory/feedback"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--user", required=True)
        parser.add_argument("--mem-cell-id")
        parser.add_argument("--memory-id")
        parser.add_argument(
            "--type", required=True, dest="type",
            choices=[
                "positive", "negative", "correction",
                "deletion_request", "explicit_confirm",
            ],
        )
        parser.add_argument("--signal-value", type=float, default=0.0)
        parser.add_argument("--comment")
        parser.add_argument("--retrieval-id")

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        if not (args.mem_cell_id or args.memory_id):
            raise UsageError("--mem-cell-id or --memory-id required")
        body = {
            "tenant_id": args.tenant,
            "user_id": args.user,
            "mem_cell_id": args.mem_cell_id,
            "memory_id": args.memory_id,
            "feedback_type": args.type,
            "signal_value": args.signal_value,
            "comment": args.comment,
            "retrieval_id": args.retrieval_id,
        }
        status, payload = await transport.request(
            "POST", "/v1/memory/feedback", json_body=body
        )
        self.check_2xx(status, payload)
        emitter.emit(payload)
        return EXIT_OK


__all__ = ["FeedbackCommand"]
