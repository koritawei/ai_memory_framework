"""CLI Transport 子包导出。"""

from __future__ import annotations

import argparse
from typing import Any

from memory_app.cli.transport.base import Transport
from memory_app.cli.transport.http import HttpTransport
from memory_app.cli.transport.local import LocalTransport


def make_transport(args: argparse.Namespace) -> Transport:
    """根据 ``--local`` / ``--server`` 选择 Transport 实例。"""
    if getattr(args, "local", False):
        return LocalTransport(admin_key=args.admin_key, api_key=args.api_key)
    return HttpTransport(
        args.server,
        admin_key=args.admin_key,
        api_key=args.api_key,
        timeout=args.timeout,
    )


__all__ = ["Transport", "HttpTransport", "LocalTransport", "make_transport"]
