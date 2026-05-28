"""ConfigCenterPromptManager —— 运行时默认实现。

═══════════════════════════════════════════════════════════════════════════════
职责
═══════════════════════════════════════════════════════════════════════════════
1. 委托 ConfigCenter 解析 prompt(五级覆盖 + 灰度路由)
2. 内置一份最近解析结果的缓存(以 ``(prompt_id, tenant_id, user_id)`` 为键)
3. 监听 ``memory.prompts.*`` 变更,自动失效缓存

═══════════════════════════════════════════════════════════════════════════════
为什么独立缓存
═══════════════════════════════════════════════════════════════════════════════
- ConfigCenter 内部的 overrides_cache(DB 后端)默认 5s TTL,而 prompt 渲染在
  冷路径 提取器单条路径上可能调 N 次,缓存可避免每次走五级合并。
- 监听 ``cc.watch`` 后,变更立即失效本地缓存 → 业务无需重启。

═══════════════════════════════════════════════════════════════════════════════
循环导入说明
═══════════════════════════════════════════════════════════════════════════════
本模块**仅**用 :class:`memory_app.config_center.ConfigCenter` 接口的鸭子调用
(``resolve_prompt`` / ``watch``),通过 ``TYPE_CHECKING`` 避免运行时硬依赖,
打破 ``config_center → prompt_manager → config_center`` 的潜在循环。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .manager import _format_template
from .models import ResolvedPromptConfig

if TYPE_CHECKING:  # pragma: no cover —— 仅类型提示用
    from memory_app.config_center.base import ConfigCenter, ConfigChangeEvent

logger = logging.getLogger(__name__)


class ConfigCenterPromptManager:
    """以 ConfigCenter 为真值源的 PromptManager。"""

    def __init__(self, config_center: "ConfigCenter") -> None:
        self._cc = config_center
        # 缓存键:(prompt_id, tenant_id_or_*, user_id_or_*)
        self._cache: dict[tuple[str, str, str], ResolvedPromptConfig] = {}
        self._cache_lock = asyncio.Lock()
        self._watch_attached = False
        # 缓存代:每次 invalidate 自增 1;resolve 期间被失效,write-back 须跳过。
        # 否则:协程 A miss → await resolve_prompt(慢);中间配置变更触发 invalidate
        # 清空 cache;A 恢复后 self._cache[key] = stale 又把旧数据回写回去。
        self._cache_gen: int = 0

    # ════════════════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════════════════
    async def attach_watcher(self) -> None:
        """订阅 ConfigCenter 变更,自动失效相关缓存。

        多次调用幂等。
        """
        if self._watch_attached:
            return
        await self._cc.watch(self._on_config_change)
        self._watch_attached = True

    async def _on_config_change(self, event: "ConfigChangeEvent") -> None:
        """监听回调:遇 ``memory.prompts.*`` 变更或全局重载即清缓存。"""
        # 延迟 import 避免循环
        from memory_app.config_center.prompt_paths import is_prompt_category

        if event.category == "*" or is_prompt_category(event.category):
            async with self._cache_lock:
                if event.category == "*":
                    self._cache.clear()
                else:
                    # 只清该 prompt_id 相关的缓存
                    from memory_app.config_center.prompt_paths import parse_prompt_id

                    pid = parse_prompt_id(event.category)
                    if pid is not None:
                        keys_to_drop = [k for k in self._cache if k[0] == pid]
                        for k in keys_to_drop:
                            self._cache.pop(k, None)
                # 把任何 in-flight resolve 的 write-back 标记为陈旧
                self._cache_gen += 1
            logger.debug("prompt cache invalidated due to %s", event.category)

    # ════════════════════════════════════════════════════════════════════════
    # 解析
    # ════════════════════════════════════════════════════════════════════════
    async def resolve(
        self,
        prompt_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        request_override: dict[str, Any] | None = None,
    ) -> ResolvedPromptConfig:
        """异步解析 prompt(五级覆盖 + 灰度)。"""
        # request_override 跳过缓存(每次内容可能不同)
        if request_override:
            return await self._cc.resolve_prompt(  # type: ignore[attr-defined]
                prompt_id,
                tenant_id=tenant_id,
                user_id=user_id,
                request_override=request_override,
            )

        cache_key = (prompt_id, tenant_id or "*", user_id or "*")
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
            # 记录此次 resolve 启动时的代号;写回前再核对 ——
            # 若期间 _on_config_change 自增代号,说明缓存已被外部主动失效,
            # 这次 resolve 拿到的可能是相对于新版本的"陈旧"快照,跳过写回。
            gen_at_start = self._cache_gen

        resolved = await self._cc.resolve_prompt(  # type: ignore[attr-defined]
            prompt_id, tenant_id=tenant_id, user_id=user_id
        )
        async with self._cache_lock:
            if self._cache_gen == gen_at_start:
                self._cache[cache_key] = resolved
            else:
                logger.debug(
                    "prompt cache write-back skipped for %s (invalidated mid-resolve)",
                    prompt_id,
                )
        return resolved

    # ════════════════════════════════════════════════════════════════════════
    # 渲染
    # ════════════════════════════════════════════════════════════════════════
    async def render(self, prompt_id: str, **variables: Any) -> str:
        """无租户 / 用户上下文的渲染。"""
        resolved = await self.resolve(prompt_id)
        return _format_template(resolved.template, resolved.variables, variables)

    async def render_for(
        self,
        prompt_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        **variables: Any,
    ) -> str:
        """带租户 / 用户上下文的渲染——冷路径+ 提取器的标准入口。

        :raises KeyError: prompt_id 不存在(且无内置种子)
        :raises ValueError: variables 与模板占位符不一致
        """
        resolved = await self.resolve(prompt_id, tenant_id=tenant_id, user_id=user_id)
        return _format_template(resolved.template, resolved.variables, variables)

    # ════════════════════════════════════════════════════════════════════════
    # 列表与失效
    # ════════════════════════════════════════════════════════════════════════
    async def list_prompts(self, tag: str | None = None) -> list[str]:
        """枚举所有可见 prompt_id。委托给 ConfigCenter 的 list_prompt_ids。"""
        return await self._cc.list_prompt_ids(tag=tag)  # type: ignore[attr-defined]

    async def invalidate_cache(self, prompt_id: str | None = None) -> None:
        """主动让缓存失效;``prompt_id=None`` 时全清。"""
        async with self._cache_lock:
            if prompt_id is None:
                self._cache.clear()
            else:
                keys_to_drop = [k for k in self._cache if k[0] == prompt_id]
                for k in keys_to_drop:
                    self._cache.pop(k, None)


__all__ = ["ConfigCenterPromptManager"]
