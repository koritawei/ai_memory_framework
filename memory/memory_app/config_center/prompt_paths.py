"""Prompt category 路径工具。

═══════════════════════════════════════════════════════════════════════════════
作用
═══════════════════════════════════════════════════════════════════════════════
LLM Prompt 与插件配置共用 ConfigCenter,但**不能**与插件 category 冲突。
约定 ``memory.prompts.*`` 前缀:

::

    prompt_id="episode_extraction"
        ↔ category="memory.prompts.episode_extraction"

本模块提供唯一的双向转换函数,业务平面禁止自行拼字符串。
"""

from __future__ import annotations

#: Prompt category 共用前缀。修改本常量需同步 default.yaml / 测试。
PROMPT_CATEGORY_PREFIX = "memory.prompts."


def prompt_category(prompt_id: str) -> str:
    """``prompt_id`` → ``memory.prompts.{prompt_id}``。

    :raises ValueError: 空字符串 / 含 ``"."`` 等非法字符
    """
    if not prompt_id:
        raise ValueError("prompt_id 不可为空")
    if "." in prompt_id:
        # 禁止 prompt_id 含 "." —— 否则会被 ConfigResolver 误判为多级 dotted key
        raise ValueError(f"prompt_id 不能包含 '.': {prompt_id!r}")
    return f"{PROMPT_CATEGORY_PREFIX}{prompt_id}"


def parse_prompt_id(category: str) -> str | None:
    """``memory.prompts.{prompt_id}`` → ``prompt_id``;不匹配前缀返回 None。"""
    if not category.startswith(PROMPT_CATEGORY_PREFIX):
        return None
    pid = category[len(PROMPT_CATEGORY_PREFIX) :]
    return pid or None


def is_prompt_category(category: str) -> bool:
    """category 是否属于 prompt 命名空间。"""
    return parse_prompt_id(category) is not None


__all__ = [
    "PROMPT_CATEGORY_PREFIX",
    "prompt_category",
    "parse_prompt_id",
    "is_prompt_category",
]
