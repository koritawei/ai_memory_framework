"""Emitter —— CLI 输出统一收口(替代旧版 ``_emit`` 内 ``print(...)`` 散落)。

═══════════════════════════════════════════════════════════════════════════════
格式
═══════════════════════════════════════════════════════════════════════════════
- ``pretty``  缩进 JSON(默认,人类可读)
- ``json``    单行 JSON(便于 jq / 脚本管道)
- ``raw``     原文输出(string payload 不再 JSON 包装)
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from memory_app.cli.errors import UsageError

ValidFormat = str  # "pretty" | "json" | "raw"
FORMATS: tuple[ValidFormat, ...] = ("pretty", "json", "raw")


class Emitter:
    """注入式 stdout 包装,便于在测试中传 :class:`io.StringIO`。"""

    def __init__(
        self, stream: TextIO | None = None, fmt: ValidFormat = "pretty"
    ) -> None:
        self._stream = stream or sys.stdout
        if fmt not in FORMATS:
            raise UsageError(f"invalid output format: {fmt!r}; choose from {FORMATS}")
        self.fmt = fmt

    def emit(self, payload: Any) -> None:
        """按 :attr:`fmt` 写入 :attr:`stream`。

        - ``raw``: str payload 透传,其它走 json.dumps
        - ``json``: 任何 payload(含 str)都 ``json.dumps`` —— 否则 ``jq`` 等管道解析失败
        - ``pretty``: 缩进 JSON
        """
        text: str
        if self.fmt == "raw":
            # raw 模式专为 grep / 直接 echo:str 透传,其它转 JSON
            text = payload if isinstance(payload, str) else json.dumps(
                payload, ensure_ascii=False
            )
        elif self.fmt == "json":
            # 关键:str payload 必须 dumps 成 JSON 字符串字面量(带引号),
            # 否则 `memory ... --output json | jq .` 会拒收裸字符串
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        else:  # pretty
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        print(text, file=self._stream)


def safe_json(body: str) -> Any:
    """容错 JSON 解析:空 body 返回 ``{}``,失败返回 ``{"raw": body}``。"""
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def read_json_arg(value: str | None) -> Any:
    """支持 ``@path/to/file.json`` 与 inline JSON;失败抛 :class:`UsageError`。"""
    if value is None:
        return None
    if value.startswith("@"):
        path = value[1:]
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except OSError as e:
            raise UsageError(f"failed to read {path}: {e}") from e
        except json.JSONDecodeError as e:
            raise UsageError(f"invalid JSON in {path}: {e}") from e
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        raise UsageError(f"invalid JSON value: {e}") from e


__all__ = ["Emitter", "FORMATS", "safe_json", "read_json_arg"]
