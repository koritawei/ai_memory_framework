"""JSON Schema 校验工具。

═══════════════════════════════════════════════════════════════════════════════
两个公共函数
═══════════════════════════════════════════════════════════════════════════════
- :func:`validate_params`  对参数 dict 做 JSON Schema 校验
- :func:`fill_defaults`    根据 schema ``default`` 字段补齐缺失项

设计要点
─────────────────────────────────────────────────────────────────────────────
- 使用 Draft 2020-12 —— pydantic v2 内部也用这个版本，行为一致
- 校验失败抛 :class:`ConfigValidationError`，附 JSON Pointer（``/k``、
  ``/params/weights/cqa``），便于运维快速定位
- ``schema=None`` 时直接放行 —— 脚手架 / 1 大量插件还没声明 schema 时不阻塞
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .base import ConfigValidationError


# Validator 编译开销 ~1ms 量级,且内部解析 $ref / 编译子规则。
# resolve 每次都重编译会让 PluginFactory.build / PromptManager.resolve 热路径白吃 CPU。
#
# 用 id(schema) 做 cache key —— schema 是 ``PluginMeta.config_schema`` 类属性,
# 跟随 plugin 类生命周期(进程 lifetime),id 稳定。Plugin 数量级 30,
# 简单 dict 足够;不必 LRU(总条目数固定且小)。
#
# ⚠️ 历史教训:首次写成 WeakValueDictionary,validator 被立即 GC,等价无缓存。
# 用普通 dict + 强引用,validator 一旦创建跟随 schema 共存到进程结束。
_validator_cache: dict[int, Draft202012Validator] = {}


def _get_validator(schema: dict) -> Draft202012Validator:
    key = id(schema)
    v = _validator_cache.get(key)
    if v is None:
        v = Draft202012Validator(schema)
        _validator_cache[key] = v
    return v


def validate_params(params: dict[str, Any], schema: dict | None) -> dict:
    """对 params 做 schema 校验。

    :param params: 待校验的参数 dict
    :param schema: JSON Schema 字典；为 None 时直接放行
    :returns: 校验通过的 params（原值返回）
    :raises ConfigValidationError: 校验失败，含首个错误的 JSON Pointer
    """
    if schema is None:
        return params
    validator = _get_validator(schema)
    # 按 JSON Pointer 路径排序：让"最浅层 / 最先出现"的错误优先报出
    errors = sorted(validator.iter_errors(params), key=lambda e: list(e.absolute_path))
    if errors:
        first: ValidationError = errors[0]
        # 把 ['params', 'weights', 'cqa'] 拼成 "/params/weights/cqa"
        pointer = "/" + "/".join(str(p) for p in first.absolute_path)
        raise ConfigValidationError(json_pointer=pointer or "/", message=first.message)
    return params


def fill_defaults(schema: dict | None, params: dict[str, Any]) -> dict[str, Any]:
    """根据 schema 中的 ``default`` 字段为缺失项填默认值（仅顶层 properties）。

    例：schema 含 ``{"properties": {"k": {"default": 60}}}``，
    传入 ``params={}`` 时返回 ``{"k": 60}``。

    刻意不递归处理嵌套 properties —— 配置中心的 schema 应当是扁平的，
    嵌套结构通过具名 sub-key 表达。
    """
    if not schema or not isinstance(schema, dict):
        return params
    out = dict(params)
    props = schema.get("properties", {})
    for k, prop in props.items():
        if k not in out and isinstance(prop, dict) and "default" in prop:
            out[k] = prop["default"]
    return out


__all__ = ["validate_params", "fill_defaults"]
