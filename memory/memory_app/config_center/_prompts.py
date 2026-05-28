"""PromptConfigMixin —— 把 Prompt 解析/写入/历史能力混入 BaseConfigCenter
。

═══════════════════════════════════════════════════════════════════════════════
为什么用 Mixin
═══════════════════════════════════════════════════════════════════════════════
Prompt 与插件配置共享 ConfigCenter 的:
- 五级覆盖 + 灰度路由(:class:`ConfigResolver`)
- 持久化与历史(``_persist_entry`` / ``_read_history``)
- 变更通知(``_notify`` / ``watch``)

但**不共享**插件特有的 ``plugin_registry.get(category, name).meta.config_schema``
校验路径——Prompt 用独立的 :func:`validate_prompt_body`。

把这套独立流程作为 Mixin 注入 :class:`BaseConfigCenter`,所有具体后端
(File / Mongo / 未来 PG)自动获得 prompt 能力,无需各自重新实现。

═══════════════════════════════════════════════════════════════════════════════
解析链(与插件保持一致)
═══════════════════════════════════════════════════════════════════════════════
::

    default → global → tenant → user → request → variants(首个 match 命中)

变体合并语义见 :class:`ConfigResolver._apply_variants`:
overlay 显式带 ``variants`` 时整体替换基座列表。

═══════════════════════════════════════════════════════════════════════════════
持久化复用 _persist_entry
═══════════════════════════════════════════════════════════════════════════════
Prompt 在持久化层用 entry 形式表达:

::

    entry = {
      "name": prompt_id,                  # ← 用 prompt_id 占位 plugin name
      "params": {                         # ← prompt body 大部分字段塞进 params
        "template": "...",
        "variables": [...],
        "description": "...",
        "version": "...",
        "tags": [...],
      },
      "variants": [...],                  # ← 与插件 entry.variants 同结构
    }

这样 FileConfigCenter / MongoConfigCenter 已实现的 ``_persist_entry``
**无需任何修改**就能写入 prompt;后端层完全不感知 plugin / prompt 的区别。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from memory_app.prompt_manager.builtins import BUILTIN_PROMPTS
from memory_app.prompt_manager.models import PromptSpec, ResolvedPromptConfig

from .base import ConfigChangeEvent, ConfigValidationError
from .prompt_paths import (
    PROMPT_CATEGORY_PREFIX,
    parse_prompt_id,
    prompt_category,
)
from .prompt_schema import validate_prompt_body

logger = logging.getLogger(__name__)


class PromptNotFoundError(LookupError):
    """指定 prompt_id 在五级覆盖与内置种子中均未命中。"""

    def __init__(self, prompt_id: str) -> None:
        super().__init__(f"prompt not found: {prompt_id!r}")
        self.prompt_id = prompt_id


class PromptConfigMixin:
    """Prompt 配置的解析 / 写入 / 历史 / 列表方法。

    ⚠️ 本 Mixin 假设 self 同时是 :class:`BaseConfigCenter`,会访问以下属性:

    - ``self._lock``               ── 写路径串行化锁
    - ``self._defaults_flat``      ── 默认值表(扁平 dotted-key dict)
    - ``self._resolver``           ── :class:`ConfigResolver` 实例
    - ``self._version``            ── 全局快照版本号
    - ``self._load_overrides``   ── 子类 hook,加载三级 overrides
    - ``self._persist_entry(...)`` ── 子类 hook,持久化一条 entry
    - ``self._read_history(...)``  ── 子类 hook,读取历史
    - ``self._notify(...)``        ── 派发变更事件
    """

    # ════════════════════════════════════════════════════════════════════════
    # 解析
    # ════════════════════════════════════════════════════════════════════════
    async def resolve_prompt(
        self,
        prompt_id: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_override: Optional[dict] = None,
    ) -> ResolvedPromptConfig:
        """按五级覆盖 + 灰度规则解析 prompt。

        若 ConfigCenter 与 ``defaults`` 中均无该 ``prompt_id``,回退到
        :data:`BUILTIN_PROMPTS` 内置种子;若仍无则抛 :class:`PromptNotFoundError`。
        """
        cat = prompt_category(prompt_id)

        # request_override 沿用插件路径的 dotted-category dict 形式
        req_for_resolver = None
        if request_override:
            req_for_resolver = {cat: {"params": request_override}}

        async with self._lock:  # type: ignore[attr-defined]
            g, t, u = await self._load_overrides()  # type: ignore[attr-defined]
            cfg, source = self._resolver.resolve(  # type: ignore[attr-defined]
                cat,
                defaults=self._defaults_flat,  # type: ignore[attr-defined]
                global_overrides=g,
                tenant_overrides=t,
                user_overrides=u,
                request_override=req_for_resolver,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            version = self._version  # type: ignore[attr-defined]

        # ConfigResolver 命中 variant 时会把 _variant_matched=True 注入 cfg
        if cfg.get("_variant_matched"):
            source = "variant"

        # entry 形态:{"name": prompt_id, "params": {body...}}
        body = self._extract_prompt_body(cfg)

        if not body:
            # 五级 / 灰度均未命中 → 回退内置种子(source=builtin)
            spec = BUILTIN_PROMPTS.get(prompt_id)
            if spec is None:
                raise PromptNotFoundError(prompt_id)
            return ResolvedPromptConfig(
                prompt_id=prompt_id,
                template=spec.template,
                variables=list(spec.variables),
                description=spec.description,
                version=spec.version,
                tags=list(spec.tags),
                config_version=version,
                source="builtin",
            )

        # 校验已落库 / 默认值的 body —— 防御历史脏数据
        try:
            body = validate_prompt_body(body)
        except ConfigValidationError as e:
            logger.warning(
                "stored prompt body for %s invalid (%s); falling back to builtin",
                prompt_id, e.message,
            )
            spec = BUILTIN_PROMPTS.get(prompt_id)
            if spec is None:
                raise PromptNotFoundError(prompt_id) from e
            return ResolvedPromptConfig(
                prompt_id=prompt_id,
                template=spec.template,
                variables=list(spec.variables),
                description=spec.description,
                version=spec.version,
                tags=list(spec.tags),
                config_version=version,
                source="builtin",
            )

        return ResolvedPromptConfig(
            prompt_id=prompt_id,
            template=body["template"],
            variables=list(body.get("variables", []) or []),
            description=body.get("description", "") or "",
            version=body.get("version", "1.0.0") or "1.0.0",
            tags=list(body.get("tags", []) or []),
            config_version=version,
            source=source,
        )

    # ════════════════════════════════════════════════════════════════════════
    # 写入
    # ════════════════════════════════════════════════════════════════════════
    async def write_prompt(
        self,
        prompt_id: str,
        body: dict[str, Any],
        scope: str = "global",
        scope_id: Optional[str] = None,
        actor: str = "ops",
    ) -> int:
        """写入 prompt overrides。返回新快照版本号。

        ``body`` 含 ``template`` / ``variables`` / 可选 ``variants`` 等字段;
        校验由 :func:`validate_prompt_body` 完成。
        """
        if scope not in ("global", "tenant", "user"):
            raise ValueError(f"invalid scope: {scope!r}")
        if scope in ("tenant", "user") and not scope_id:
            raise ValueError(f"scope={scope!r} requires non-empty scope_id")

        body = validate_prompt_body(body)

        cat = prompt_category(prompt_id)
        # 把 body 拆成 params + variants,与 plugin entry 同构
        variants = body.pop("variants", None)
        entry: dict[str, Any] = {
            "name": prompt_id,
            "params": body,  # template / variables / description / version / tags
        }
        if variants is not None:
            entry["variants"] = variants

        async with self._lock:  # type: ignore[attr-defined]
            new_version = await self._persist_entry(  # type: ignore[attr-defined]
                category=cat,
                scope=scope,
                scope_id=scope_id,
                entry=entry,
                actor=actor,
            )
            self._version = max(self._version + 1, new_version)  # type: ignore[attr-defined]

        event = ConfigChangeEvent(
            category=cat,
            scope=scope,
            scope_id=scope_id,
            name=prompt_id,
            version=self._version,  # type: ignore[attr-defined]
            actor=actor,
        )
        await self._notify(event)  # type: ignore[attr-defined]
        return self._version  # type: ignore[attr-defined]

    # ════════════════════════════════════════════════════════════════════════
    # 历史
    # ════════════════════════════════════════════════════════════════════════
    async def history_prompt(self, prompt_id: str, limit: int = 50) -> list[dict]:
        """返回某 prompt 的历史版本(最新优先)。复用 :meth:`_read_history`。"""
        cat = prompt_category(prompt_id)
        return await self._read_history(cat, limit)  # type: ignore[attr-defined]

    # ════════════════════════════════════════════════════════════════════════
    # 列表
    # ════════════════════════════════════════════════════════════════════════
    async def list_prompt_ids(
        self,
        *,
        include_builtin: bool = True,
        tag: str | None = None,
    ) -> list[str]:
        """枚举所有可见 prompt_id。

        来源:
        - 五级覆盖中的 global 层(从 ``_load_overrides`` 拿)
        - ``defaults`` 中以 ``memory.prompts.`` 开头的 category
        - ``include_builtin=True`` 时合并 :data:`BUILTIN_PROMPTS`
        """
        ids: set[str] = set()

        async with self._lock:  # type: ignore[attr-defined]
            g, _, _ = await self._load_overrides()  # type: ignore[attr-defined]
            for cat in g.keys():
                pid = parse_prompt_id(cat)
                if pid:
                    ids.add(pid)
            for cat in self._defaults_flat.keys():  # type: ignore[attr-defined]
                pid = parse_prompt_id(cat)
                if pid:
                    ids.add(pid)

        if include_builtin:
            ids.update(BUILTIN_PROMPTS.keys())

        if tag is None:
            return sorted(ids)

        # tag 过滤需要解析每条 → 调 resolve_prompt 逐个查
        result: list[str] = []
        for pid in sorted(ids):
            try:
                resolved = await self.resolve_prompt(pid)
            except Exception:  # noqa: BLE001
                continue
            if tag in resolved.tags:
                result.append(pid)
        return result

    # ════════════════════════════════════════════════════════════════════════
    # 内部辅助
    # ════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _extract_prompt_body(cfg: dict[str, Any]) -> dict[str, Any] | None:
        """把 ConfigResolver 解析出的 entry/cfg 还原为 prompt body。

        优先级:
        1. cfg 直接带 template -> 视为已是 body(default.yaml 直接写顶层 template 的写法)
        2. cfg 含 ``params``   -> body = {**params,**(其他元数据)}
        3. 否则返回 None
        """
        if not cfg:
            return None
        if "template" in cfg:
            # default.yaml 顶层风格: { template, variables, variants, ... }
            body = {k: v for k, v in cfg.items() if k != "_variant_matched"}
            body.pop("name", None)
            return body
        if "params" in cfg and isinstance(cfg["params"], dict):
            body = dict(cfg["params"])
            return body
        return None


__all__ = [
    "PromptConfigMixin",
    "PromptNotFoundError",
    "PROMPT_CATEGORY_PREFIX",
]
