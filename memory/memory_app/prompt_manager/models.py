"""Prompt 公共数据模型。

═══════════════════════════════════════════════════════════════════════════════
两个数据模型
═══════════════════════════════════════════════════════════════════════════════
- :class:`PromptSpec`              Prompt 静态规格(template + variables + 元数据)
- :class:`ResolvedPromptConfig`    解析结果(含命中层 source 标签)

设计要点
─────────────────────────────────────────────────────────────────────────────
- ``template`` 用 Python ``str.format`` —— 与  表格一致;
  JSON 等花括号必须自行转义为 ``{{`` / ``}}``。
- ``variants[]`` 与 :class:`memory_app.config_center.ConfigResolver`
  的 5 维 ``match`` 共用语义,见 ``_match_gray_rule``。
- ``ResolvedPromptConfig.source`` 标签用于运维排查"为什么这个用户走了
  Acme 模板"。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PromptSpec(BaseModel):
    """Prompt 静态规格(写入 ConfigCenter 的 body)。"""

    model_config = ConfigDict(extra="allow")

    #: Python ``str.format`` 模板字符串。占位符如 ``{text}`` 必须在
    #: ``variables`` 中声明,便于校验与 Admin 试渲染。
    template: str

    #: 模板中的占位符名列表。Admin ``render`` 调用时用于校验入参。
    variables: list[str] = Field(default_factory=list)

    description: str = ""
    version: str = "1.0.0"
    tags: list[str] = Field(default_factory=list)

    #: 灰度变体;首个 ``match`` 命中后整体替换 ``template`` / ``variables``。
    #: 与 :class:`memory_app.config_center.ConfigResolver` 的灰度语义共用。
    variants: list[dict[str, Any]] | None = None


class ResolvedPromptConfig(BaseModel):
    """:meth:`PromptConfigMixin.resolve_prompt` 的返回值。"""

    model_config = ConfigDict(extra="allow")

    #: 业务侧 prompt 标识,如 ``"episode_extraction"``
    prompt_id: str

    #: 已解析的最终模板字符串(含 variants 选择后的覆盖)
    template: str

    #: 占位符名列表
    variables: list[str] = Field(default_factory=list)

    description: str = ""
    version: str = "1.0.0"
    tags: list[str] = Field(default_factory=list)

    #: 配置版本号(每次 write_prompt 自增;客户端可据此做缓存失效)
    config_version: int = 0

    #: 命中层 / 来源标签:default / global / tenant / user / request / variant
    source: str = "default"


__all__ = ["PromptSpec", "ResolvedPromptConfig"]
