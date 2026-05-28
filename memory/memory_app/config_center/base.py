"""ConfigCenter 抽象基类与公共数据模型。

═══════════════════════════════════════════════════════════════════════════════
本模块只定义"接口契约"，不含任何实现
═══════════════════════════════════════════════════════════════════════════════
- :class:`ConfigCenter`            最顶层 ABC，所有后端共同契约
- :class:`ResolvedPluginConfig`    resolve 返回值（含 source 来源标签）
- :class:`ConfigChangeEvent`       变更事件载荷（watch 推送给监听者）
- :class:`ConfigValidationError`   Schema 校验失败异常（含 JSON Pointer 定位）
- ``ConfigChangeCallback``         监听回调签名（async）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from memory_app._compat import utcnow


class ResolvedPluginConfig(BaseModel):
    """:meth:`ConfigCenter.resolve` 的返回值。"""

    #: 解析出的插件名（业务方据此从 PluginRegistry 取类）
    name: str

    #: 已经过 schema 校验、填好默认值的参数 dict
    params: dict

    #: 配置版本号（每次写入自增；客户端可据此做缓存失效）
    version: int = 0

    #: 命中层来源标签：default / global / tenant / user / request
    #: 主要用于运维排查"为什么这个用户走了 hybrid_sbd"
    source: str = "default"


class ConfigChangeEvent(BaseModel):
    """配置变更事件，由 watch 推送给监听者（如 :class:`PluginFactory`）。"""

    #: 变更的 category；``"*"`` 表示全局变更（File backend 重载整个 YAML 时）
    category: str

    #: 变更影响的 scope：global / tenant / user
    scope: str = "global"

    #: scope=tenant/user 时为对应 ID；scope=global 时为 None
    scope_id: Optional[str] = None

    #: 变更后的插件名（如改 name 切换实现），category=``"*"`` 时为 None
    name: str | None = None

    #: 写入后的快照版本号
    version: int = 0

    #: 操作者标识（用于审计）
    actor: str = "system"

    #: 事件产生时刻（UTC）
    timestamp: datetime = Field(default_factory=utcnow)


class ConfigValidationError(Exception):
    """配置写入时 schema 校验失败。

    错误信息附带 JSON Pointer 定位（如 ``/params/k``），便于运维快速找到
    哪个字段不合法。
    """

    def __init__(self, json_pointer: str, message: str) -> None:
        self.json_pointer = json_pointer
        self.message = message
        super().__init__(f"{json_pointer}: {message}")


#: 监听回调签名：async function 接收变更事件，无返回值
ConfigChangeCallback = Callable[[ConfigChangeEvent], Awaitable[None]]


class ConfigCenter(ABC):
    """统一配置中心接口（顶层 ABC）。

    具体后端不应直接继承本类（除非真正全自定义），而应继承
    :class:`memory_app.config_center.BaseConfigCenter` 或
    :class:`memory_app.config_center.DBConfigCenter` 复用通用流程。
    """

    @abstractmethod
    async def resolve(
        self,
        category: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_override: Optional[dict] = None,
    ) -> ResolvedPluginConfig:
        """按五级覆盖（default → global → tenant → user → request）解析有效配置。"""

    @abstractmethod
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
        """写入配置；返回新版本号。

        :raises ConfigValidationError: ``params`` 不符合插件 schema
        """

    @abstractmethod
    async def history(self, category: str, limit: int = 50) -> list[dict]:
        """返回历史版本列表（最新优先）。"""

    @abstractmethod
    async def watch(self, callback: ConfigChangeCallback) -> None:
        """订阅配置变更。

        callback 异常应被吞掉并打 warn —— 单个监听者错误不应影响其他监听者。
        """

    # ── 默认实现（子类可覆盖） ──
    async def health(self) -> dict:
        """健康检查。返回 ``{"status": "ok|degraded|fail", "detail": "..."}``。"""
        return {"status": "ok"}

    async def close(self) -> None:
        """优雅关闭。默认 no-op；持有外部资源的子类必须覆盖。"""
        return None


__all__ = [
    "ConfigCenter",
    "ConfigValidationError",
    "ConfigChangeEvent",
    "ConfigChangeCallback",
    "ResolvedPluginConfig",
]
