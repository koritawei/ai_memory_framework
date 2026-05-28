"""``memory prompt list|get|history|set|delete|render`` —— /v1/admin/prompts/*。"""

from __future__ import annotations

import argparse
from typing import Any, ClassVar

from memory_app.cli.commands.base import Command
from memory_app.cli.errors import EXIT_OK, UsageError
from memory_app.cli.output import Emitter, read_json_arg
from memory_app.cli.transport import Transport


class PromptCommand(Command):
    name: ClassVar[str] = "prompt"
    help: ClassVar[str] = "/v1/admin/prompts/*"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        sub = parser.add_subparsers(
            dest="prompt_action", required=True, metavar="<action>"
        )
        ll = sub.add_parser("list", help="列出可见 prompt_id")
        ll.add_argument("--tag")
        ll.add_argument(
            "--include-builtin",
            default=True,
            type=lambda v: str(v).lower() != "false",
        )

        gg = sub.add_parser("get", help="解析 prompt 当前生效内容")
        gg.add_argument("prompt_id")
        gg.add_argument("--tenant-id")
        gg.add_argument("--user-id")

        hh = sub.add_parser("history", help="历史变更(最新优先)")
        hh.add_argument("prompt_id")
        hh.add_argument("--limit", type=int, default=50)

        ss = sub.add_parser("set", help="写入 / 更新 prompt 覆盖")
        ss.add_argument("prompt_id")
        ss.add_argument("--template", required=True)
        ss.add_argument("--variables", nargs="*", default=None)
        ss.add_argument("--tags", nargs="*", default=None)
        ss.add_argument("--description")
        ss.add_argument("--version")
        ss.add_argument("--variants", help="JSON 数组(@file 或 inline)")
        ss.add_argument("--scope", default="global", choices=["global", "tenant", "user"])
        ss.add_argument("--scope-id")
        ss.add_argument("--actor", default="cli")

        dd = sub.add_parser("delete", help="清除指定 scope 的 prompt 覆盖")
        dd.add_argument("prompt_id")
        dd.add_argument("--scope", default="global", choices=["global", "tenant", "user"])
        dd.add_argument("--scope-id")
        dd.add_argument("--actor", default="cli")

        rn = sub.add_parser("render", help="按当前覆盖试渲染")
        rn.add_argument("prompt_id")
        rn.add_argument("--vars", help="JSON 对象(@file 或 inline)", default="{}")
        rn.add_argument("--tenant-id")
        rn.add_argument("--user-id")

    async def run(
        self,
        args: argparse.Namespace,
        transport: Transport,
        emitter: Emitter,
    ) -> int:
        action = args.prompt_action
        if action == "list":
            status, payload = await transport.request(
                "GET", "/v1/admin/prompts",
                query={
                    "tag": args.tag,
                    "include_builtin": str(args.include_builtin).lower(),
                },
                admin=True,
            )
        elif action == "get":
            status, payload = await transport.request(
                "GET", f"/v1/admin/prompts/{args.prompt_id}",
                query={"tenant_id": args.tenant_id, "user_id": args.user_id},
                admin=True,
            )
        elif action == "history":
            status, payload = await transport.request(
                "GET", f"/v1/admin/prompts/{args.prompt_id}/history",
                query={"limit": args.limit}, admin=True,
            )
        elif action == "set":
            body: dict[str, Any] = {"template": args.template}
            if args.variables:
                body["variables"] = args.variables
            if args.tags:
                body["tags"] = args.tags
            if args.description:
                body["description"] = args.description
            if args.version:
                body["version"] = args.version
            if args.variants:
                vr = read_json_arg(args.variants)
                if not isinstance(vr, list):
                    raise UsageError("--variants must be JSON array")
                body["variants"] = vr
            status, payload = await transport.request(
                "PUT", f"/v1/admin/prompts/{args.prompt_id}",
                query={
                    "scope": args.scope,
                    "scope_id": args.scope_id,
                    "actor": args.actor,
                },
                json_body=body, admin=True,
            )
        elif action == "delete":
            status, payload = await transport.request(
                "DELETE", f"/v1/admin/prompts/{args.prompt_id}",
                query={
                    "scope": args.scope,
                    "scope_id": args.scope_id,
                    "actor": args.actor,
                },
                admin=True,
            )
        else:  # render
            variables = read_json_arg(args.vars) or {}
            if not isinstance(variables, dict):
                raise UsageError("--vars must be JSON object")
            status, payload = await transport.request(
                "POST", f"/v1/admin/prompts/{args.prompt_id}/render",
                query={
                    "tenant_id": args.tenant_id,
                    "user_id": args.user_id,
                },
                json_body={"variables": variables},
                admin=True,
            )
        self.check_2xx(status, payload)
        emitter.emit(payload)
        return EXIT_OK


__all__ = ["PromptCommand"]
