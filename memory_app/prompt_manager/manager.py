"""StandalonePromptManager —— 不依赖 ConfigCenter 的最小 PromptManager 实现。

═══════════════════════════════════════════════════════════════════════════════
什么时候用
═══════════════════════════════════════════════════════════════════════════════
- 单元测试场景:不想拉起 ConfigCenter 也能 ``render(...)``
- 进程 boostrap 极早期:ConfigCenter 还未 init,但 logger / 健康检查需要 prompt
- 离线脚本(数据修复 / 评测):无需热更新,直接读 :data:`BUILTIN_PROMPTS`

行为
─────────────────────────────────────────────────────────────────────────────
- ``render`` / ``render_for`` 直接走内置种子 + ``register`` 时注入的覆盖
- 不监听任何外部变更
- 不参与五级覆盖、灰度路由(返回 ``source="builtin"`` 或 ``"override"``)
"""

from __future__ import annotations

import logging
from typing import Any

from .builtins import BUILTIN_PROMPTS
from .models import PromptSpec, ResolvedPromptConfig

logger = logging.getLogger(__name__)


class StandalonePromptManager:
    """最小可用的 PromptManager。"""

    def __init__(self, *, include_builtin: bool = True) -> None:
        self._overrides: dict[str, PromptSpec] = {}
        self._include_builtin = include_builtin

    # ════════════════════════════════════════════════════════════════════════
    # 注册 / 覆盖
    # ════════════════════════════════════════════════════════════════════════
    def register(self, prompt_id: str, spec: PromptSpec | dict[str, Any]) -> None:
        """注册 / 覆盖一个 prompt。

        ``spec`` 可以是 :class:`PromptSpec` 或等价 dict。
        """
        if isinstance(spec, dict):
            spec = PromptSpec.model_validate(spec)
        self._overrides[prompt_id] = spec

    def unregister(self, prompt_id: str) -> None:
        self._overrides.pop(prompt_id, None)

    # ════════════════════════════════════════════════════════════════════════
    # 解析
    # ════════════════════════════════════════════════════════════════════════
    def list_prompts(self) -> list[str]:
        ids = set(self._overrides.keys())
        if self._include_builtin:
            ids.update(BUILTIN_PROMPTS.keys())
        return sorted(ids)

    def resolve(self, prompt_id: str) -> ResolvedPromptConfig:
        """同步解析,不查 ConfigCenter。"""
        spec: PromptSpec | None = self._overrides.get(prompt_id)
        source = "override"
        if spec is None and self._include_builtin:
            spec = BUILTIN_PROMPTS.get(prompt_id)
            source = "builtin"
        if spec is None:
            raise KeyError(f"prompt not found: {prompt_id!r}")
        return ResolvedPromptConfig(
            prompt_id=prompt_id,
            template=spec.template,
            variables=list(spec.variables),
            description=spec.description,
            version=spec.version,
            tags=list(spec.tags),
            config_version=0,
            source=source,
        )

    # ════════════════════════════════════════════════════════════════════════
    # 渲染
    # ════════════════════════════════════════════════════════════════════════
    def render(self, prompt_id: str, **variables: Any) -> str:
        """同步渲染。

        :raises KeyError: prompt_id 未注册
        :raises ValueError: ``variables`` 不满足模板占位符要求
        """
        resolved = self.resolve(prompt_id)
        return _format_template(resolved.template, resolved.variables, variables)

    async def render_for(
        self,
        prompt_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        **variables: Any,
    ) -> str:
        """异步签名,与 :class:`ConfigCenterPromptManager.render_for` 一致。

        本实现忽略 ``tenant_id`` / ``user_id``(无 ConfigCenter 支撑灰度)。
        """
        return self.render(prompt_id, **variables)


# ════════════════════════════════════════════════════════════════════════════
# 公共渲染工具
# ════════════════════════════════════════════════════════════════════════════
def _format_template(template: str, variables: list[str], values: dict[str, Any]) -> str:
    """安全 format:校验占位符 + 缺失即抛 ValueError。

    Python ``str.format`` 默认对缺失键抛 KeyError;此函数把它转成更友好的
    ``ValueError`` 并显式列出缺失字段。
    """
    missing = [v for v in variables if v not in values]
    if missing:
        raise ValueError(
            f"missing prompt variables: {missing}; required={variables}"
        )
    try:
        return template.format(**values)
    except KeyError as e:
        # template 里出现但 variables 没声明的占位符
        raise ValueError(f"template references undeclared variable {e}") from e
    except IndexError as e:
        raise ValueError(f"template format error: {e}") from e


__all__ = ["StandalonePromptManager", "_format_template"]
