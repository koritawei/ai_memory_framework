"""``memory health`` —— GET /health/ready 聚合状态。"""

from __future__ import annotations

import argparse
from typing import ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_BUSINESS, EXIT_OK
from memory_app.cli.output import Emitter
from memory_app.cli.transport import Transport


class HealthCommand(Command):
    name: ClassVar[str] = "health"
    help: ClassVar[str] = "GET /health/ready"

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        status, payload = await transport.request("GET", "/health/ready")
        emitter.emit(payload)
        is_healthy = (
            status == 200
            and isinstance(payload, dict)
            and payload.get("status") in ("ok", "degraded")
        )
        return EXIT_OK if is_healthy else EXIT_BUSINESS


__all__ = ["HealthCommand"]
