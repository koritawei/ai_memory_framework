"""Prompt body 校验(设计文档 §2.8.4.1)。

═══════════════════════════════════════════════════════════════════════════════
为什么不复用插件 schema.py
═══════════════════════════════════════════════════════════════════════════════
Plugin schema 由 :class:`memory_app.plugins.base.PluginMeta.config_schema` 在
**注册时**声明,Prompt 没有"注册类"的概念,所以走独立的轻量校验。

校验规则
─────────────────────────────────────────────────────────────────────────────
- ``template``        必填,非空字符串
- ``variables``       可选,字符串列表
- ``description``     可选,字符串
- ``version``         可选,字符串
- ``tags``            可选,字符串列表
- ``variants``        可选,列表;每条 variant 含 ``match`` (dict) +
                      可选 ``template`` / ``variables`` 等覆盖字段
- 多余字段被允许(``extra="allow"``),便于未来扩展
"""

from __future__ import annotations

from typing import Any

from .base import ConfigValidationError


def validate_prompt_body(body: Any, *, json_pointer_prefix: str = "") -> dict[str, Any]:
    """对 Prompt body 做轻量校验。

    :param body: 待校验 dict;非 dict 直接抛 :class:`ConfigValidationError`
    :param json_pointer_prefix: 错误信息的 JSON Pointer 前缀(便于嵌套场景)
    :returns: 校验通过的 body(原 dict 浅拷贝)
    :raises ConfigValidationError: 任意字段不合法
    """
    if not isinstance(body, dict):
        raise ConfigValidationError(
            json_pointer_prefix or "/", "prompt body must be a mapping"
        )

    out = dict(body)

    template = out.get("template")
    if not isinstance(template, str) or not template:
        raise ConfigValidationError(
            f"{json_pointer_prefix}/template",
            "template is required and must be a non-empty string",
        )

    variables = out.get("variables")
    if variables is not None:
        if not isinstance(variables, list) or not all(isinstance(v, str) for v in variables):
            raise ConfigValidationError(
                f"{json_pointer_prefix}/variables",
                "variables must be a list of strings",
            )

    description = out.get("description")
    if description is not None and not isinstance(description, str):
        raise ConfigValidationError(
            f"{json_pointer_prefix}/description", "description must be string"
        )

    version = out.get("version")
    if version is not None and not isinstance(version, str):
        raise ConfigValidationError(
            f"{json_pointer_prefix}/version", "version must be string"
        )

    tags = out.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ConfigValidationError(
                f"{json_pointer_prefix}/tags", "tags must be a list of strings"
            )

    variants = out.get("variants")
    if variants is not None:
        if not isinstance(variants, list):
            raise ConfigValidationError(
                f"{json_pointer_prefix}/variants", "variants must be a list"
            )
        for i, v in enumerate(variants):
            if not isinstance(v, dict):
                raise ConfigValidationError(
                    f"{json_pointer_prefix}/variants/{i}", "each variant must be a mapping"
                )
            match = v.get("match")
            if match is not None and not isinstance(match, dict):
                raise ConfigValidationError(
                    f"{json_pointer_prefix}/variants/{i}/match",
                    "variant match must be a mapping",
                )
            # variant 自带 template 时也校验
            v_template = v.get("template")
            if v_template is not None and (
                not isinstance(v_template, str) or not v_template
            ):
                raise ConfigValidationError(
                    f"{json_pointer_prefix}/variants/{i}/template",
                    "variant template must be a non-empty string",
                )

    return out


__all__ = ["validate_prompt_body"]
