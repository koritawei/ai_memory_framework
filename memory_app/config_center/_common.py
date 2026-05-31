"""BaseConfigCenter —— 所有 ConfigCenter 后端的通用骨架。

═══════════════════════════════════════════════════════════════════════════════
角色与职责
═══════════════════════════════════════════════════════════════════════════════
把「配置中心通用流程」固化在这里，子类只需实现 4 个生命周期 hook：

- :meth:`_load_overrides`   加载三级 overrides（global / tenant / user）
- :meth:`_persist_entry`    持久化一条 entry，返回新版本号
- :meth:`_read_history`     读取历史
- :meth:`_spawn_watcher`    启动后端原生变更监听
- :meth:`_stop_watcher`     停止变更监听

通用流程（基类完成）：

- ``resolve``             5 级覆盖合并 + 灰度路由 + Schema 校验 + 默认值填充
- ``write``               Schema 前置校验 → 调子类持久化 → version 自增 → 派发事件
- ``history``             转发到子类
- ``watch`` / ``_notify`` 多 callback 派发，单个失败不影响其他
- ``close``               停 watcher

═══════════════════════════════════════════════════════════════════════════════
适用范围
═══════════════════════════════════════════════════════════════════════════════
- File / DB / 远程配置服务（Apollo / etcd / Nacos）等任意后端
- DB 类后端可继续继承 :class:`memory_app.config_center.DBConfigCenter`（``_db.py``）
  复用 9 个细粒度 CRUD 原语模板

═══════════════════════════════════════════════════════════════════════════════
并发与一致性
═══════════════════════════════════════════════════════════════════════════════
- 所有写路径共用 ``self._lock`` 串行化
- watch 通知**在锁外**派发：避免回调阻塞写入
- ``self._version`` 单调递增，作为缓存失效令牌使用
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Awaitable, Callable, Optional

from memory_app.plugins import registry as plugin_registry
from memory_app.plugins.base import PluginNotFoundError

from ._prompts import PromptConfigMixin
from .base import (
    ConfigCenter,
    ConfigChangeCallback,
    ConfigChangeEvent,
    ConfigValidationError,
    ResolvedPluginConfig,
)
from .resolver import ConfigResolver, compute_cache_user_key
from .schema import fill_defaults, validate_params

logger = logging.getLogger(__name__)


class BaseConfigCenter(PromptConfigMixin, ConfigCenter):
    """ConfigCenter 通用骨架。

    版本语义：
    - ``self._version`` 是 **快照版本**，每次成功 ``write`` 自增 1；
    - watcher 收到外部变更时也会自增（由子类调用 :meth:`_bump_version`）。

    并发：所有写路径共用 ``self._lock`` 串行化；后端的 watch 通知在锁外派发，
    避免回调阻塞写入。
    """

    def __init__(
        self,
        *,
        defaults_flat: dict | None = None,
        resolver: ConfigResolver | None = None,
    ) -> None:
        self._defaults_flat: dict = defaults_flat or {}
        self._resolver: ConfigResolver = resolver or ConfigResolver()
        self._callbacks: list[ConfigChangeCallback] = []
        self._lock: asyncio.Lock = asyncio.Lock()
        self._version: int = 0
        self._watcher_started: bool = False
        self._closed: bool = False

    # ════════════════════════════════════════════════════════════
    # 子类必须实现的 hooks
    # ════════════════════════════════════════════════════════════
    @abstractmethod
    async def _load_overrides(self) -> tuple[dict, dict, dict]:
        """加载三级 overrides。

        返回 ``(global, tenant, user)``：

        - global: ``{category: entry}``
        - tenant: ``{tenant_id: {category: entry}}``
        - user:   ``{user_id: {category: entry}}``

        其中 entry 至少含 ``name`` / ``params``，可选 ``variants``。
        """

    @abstractmethod
    async def _persist_entry(
        self,
        *,
        category: str,
        scope: str,
        scope_id: Optional[str],
        entry: dict,
        actor: str,
    ) -> int:
        """持久化一条 entry，返回**新版本号**。

        基类已在 :meth:`write` 中完成 schema 校验与互斥锁；子类只需关心存储。
        子类应同时把旧值写入历史区（如有）。
        """

    @abstractmethod
    async def _read_history(self, category: str, limit: int) -> list[dict]:
        """读取指定 category 的历史版本（最新优先）。"""

    @abstractmethod
    async def _spawn_watcher(
        self, on_native_event: Callable[[ConfigChangeEvent], Awaitable[None]]
    ) -> None:
        """启动后端原生变更监听。子类在检测到外部变更时回调 ``on_native_event``。"""

    @abstractmethod
    async def _stop_watcher(self) -> None:
        """停止变更监听并释放资源。"""

    # ════════════════════════════════════════════════════════════
    # 通用 ConfigCenter 接口（基类实现）
    # ════════════════════════════════════════════════════════════
    async def resolve(
        self,
        category: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_override: Optional[dict] = None,
    ) -> ResolvedPluginConfig:
        async with self._lock:
            g, t, u = await self._load_overrides()
            cfg, source = self._resolver.resolve(
                category,
                defaults=self._defaults_flat,
                global_overrides=g,
                tenant_overrides=t,
                user_overrides=u,
                request_override=request_override,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            version = self._version
        if not cfg:
            raise PluginNotFoundError(category, "<unset>")
        name = cfg.get("name")
        if not name:
            raise ConfigValidationError(
                "/name", f"category {category!r} has no plugin name configured"
            )
        params = cfg.get("params", {}) or {}
        # 用注册表里的 schema 填默认 + 校验
        try:
            cls = plugin_registry.get(category, name)
            schema = cls.meta.config_schema
            params = fill_defaults(schema, params)
            params = validate_params(params, schema)
        except PluginNotFoundError:
            # 测试 / 早期阶段允许 ConfigCenter 引用尚未实现的插件，仅 debug
            logger.debug(
                "plugin %s/%s not registered yet (allowed at bootstrap)", category, name
            )
        cache_user_key = compute_cache_user_key(
            user_id=user_id,
            source=source,
            variant_user_scoped=bool(cfg.get("_variant_user_scoped")),
        )
        return ResolvedPluginConfig(
            name=name,
            params=params,
            version=version,
            source=source,
            cache_user_key=cache_user_key,
        )

    async def write(
        self,
        category: str,
        name: str,
        params: dict,
        scope: str = "global",
        scope_id: str | None = None,
        actor: str = "ops",
        gray_rules: list[dict] | None = None,
    ) -> int:
        # 1. schema 校验（写入前置）
        try:
            cls = plugin_registry.get(category, name)
            params = fill_defaults(cls.meta.config_schema, params)
            params = validate_params(params, cls.meta.config_schema)
        except PluginNotFoundError as e:
            raise ConfigValidationError("/name", str(e)) from e

        if scope not in ("global", "tenant", "user"):
            raise ValueError(f"invalid scope: {scope!r}")
        if scope in ("tenant", "user") and not scope_id:
            raise ValueError(f"scope={scope!r} requires non-empty scope_id")

        entry = {"name": name, "params": params}
        if gray_rules:
            entry["variants"] = gray_rules

        # 2. 持久化（子类）
        async with self._lock:
            version = await self._persist_entry(
                category=category,
                scope=scope,
                scope_id=scope_id,
                entry=entry,
                actor=actor,
            )
            self._version = max(self._version + 1, version)

        # 3. 派发变更（锁外，避免回调阻塞写）
        event = ConfigChangeEvent(
            category=category,
            scope=scope,
            scope_id=scope_id,
            name=name,
            version=self._version,
            actor=actor,
        )
        await self._notify(event)
        return self._version

    async def history(self, category: str, limit: int = 50) -> list[dict]:
        return await self._read_history(category, limit)

    async def watch(self, callback: ConfigChangeCallback) -> None:
        # 串行化 register + spawn 防止两个并发 watch 在 ``_watcher_started=False``
        # 时各自调用 ``_spawn_watcher`` 各起一份后台任务 / Mongo Change Stream。
        async with self._lock:
            self._callbacks.append(callback)
            if not self._watcher_started:
                await self._spawn_watcher(self._notify)
                self._watcher_started = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stop_watcher()
        except Exception as e:  # noqa: BLE001
            logger.warning("stop watcher failed: %s", e)

    # ════════════════════════════════════════════════════════════
    # 内部协助
    # ════════════════════════════════════════════════════════════
    async def _notify(self, event: ConfigChangeEvent) -> None:
        """把变更事件广播给所有 callback；单个回调异常不影响其他。"""
        for cb in list(self._callbacks):
            try:
                await cb(event)
            except Exception as e:  # noqa: BLE001
                logger.warning("config change callback failed: %s", e)

    def _bump_version(self) -> int:
        """子类在感知到外部变更时调用，自增快照版本并返回新值。"""
        self._version += 1
        return self._version

    def set_defaults(self, defaults_flat: dict) -> None:
        """让子类在初始化后/重载时更新 defaults。"""
        self._defaults_flat = defaults_flat


__all__ = ["BaseConfigCenter"]
