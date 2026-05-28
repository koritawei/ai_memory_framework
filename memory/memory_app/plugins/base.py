"""Plugin SPI 公共基类与公共数据模型。

═══════════════════════════════════════════════════════════════════════════════
本模块提供四种公共构件
═══════════════════════════════════════════════════════════════════════════════
- :class:`Plugin`         所有 SPI 实现的父类（生命周期 + 健康 + reload）
- :class:`PluginMeta`     插件静态元信息（name / category / version / config_schema）
- :class:`PluginError`    插件内部错误的统一包装（含 retryable 标志）
- 异常类层级：``PluginConflictError`` / ``PluginNotFoundError``

═══════════════════════════════════════════════════════════════════════════════
为什么需要 Plugin 基类
═══════════════════════════════════════════════════════════════════════════════
1. **统一生命周期**：所有插件必须 ``await start(config)`` / ``await stop``，
   让框架可以在配置变更时无差别 reload。
2. **统一健康检查**：``health`` 由 ``/v1/admin/plugins/health`` 直接聚合返回。
3. **统一错误约定**：内部异常包装为 :class:`PluginError`，框架统一映射到 HTTP / DLQ。
4. **静态元信息**：:attr:`Plugin.meta` 在类层声明，让框架在 ``import`` 时就能
   构建注册表，无需先实例化（避免循环依赖）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict


# ════════════════════════════════════════════════════════════════════════════
# 错误类型
# ════════════════════════════════════════════════════════════════════════════
class PluginErrorCategory(str, Enum):
    """插件错误大类。框架按此映射到不同的 HTTP code / 重试策略。"""

    CONFIG = "config"           # 配置不合法 → 400
    DEPENDENCY = "dependency"   # 外部依赖不可用 → 503
    TIMEOUT = "timeout"         # 超时 → 504（通常 retryable=True）
    INTERNAL = "internal"       # 插件内部 bug → 500
    NOT_FOUND = "not_found"     # 注册表查不到 → 500（通常是配置错误）
    CONFLICT = "conflict"       # 重复注册同名插件 → 启动期阻断


class PluginError(Exception):
    """所有插件内部错误必须包装为本类抛出。

    框架据此决定：
    - 是否重试（``retryable=True`` → DLQ + 指数退避）
    - HTTP 状态码映射（按 :class:`PluginErrorCategory`）
    - 是否触发降级模式（``DEPENDENCY`` →  降级表）
    """

    def __init__(
        self,
        category: PluginErrorCategory | str,
        code: str,
        message: str = "",
        retryable: bool = False,
        cause: Optional[BaseException] = None,
    ) -> None:
        # 字符串形式的 category 自动转 Enum，便于 raise PluginError("internal", ...) 风格
        if isinstance(category, str):
            try:
                category = PluginErrorCategory(category)
            except ValueError:
                category = PluginErrorCategory.INTERNAL
        self.category = category
        self.code = code
        self.message = message
        self.retryable = retryable
        if cause is not None:
            # 保留 traceback chain，方便排查
            self.__cause__ = cause
        super().__init__(f"[{category.value}/{code}] {message}")


class PluginConflictError(PluginError):
    """同 ``(category, name)`` 重复注册触发。属启动期错误，应阻止进程上线。"""

    def __init__(self, category: str, name: str) -> None:
        super().__init__(
            category=PluginErrorCategory.CONFLICT,
            code="duplicate_registration",
            message=f"plugin already registered: {category}/{name}",
        )


class PluginNotFoundError(PluginError, LookupError):
    """注册表中找不到指定插件。

    继承 :class:`LookupError` 是为了兼容 ``pytest.raises(LookupError)`` 之类的
    通用检查 —— 调用方既可以捕获插件特定异常，也可以用标准库异常做防御。
    """

    def __init__(self, category: str, name: str) -> None:
        # 显式调 PluginError.__init__，避免 LookupError.__init__ 抢先吃掉参数
        PluginError.__init__(
            self,
            category=PluginErrorCategory.NOT_FOUND,
            code="plugin_not_found",
            message=f"plugin not found: {category}/{name}",
        )


# ════════════════════════════════════════════════════════════════════════════
# 元信息
# ════════════════════════════════════════════════════════════════════════════
class PluginMeta(BaseModel):
    """插件静态元信息（注册时必须声明）。

    设计为 ``frozen=True`` —— 注册后不可变，避免运行中被意外篡改。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 全局唯一插件名（在同一 ``category`` 下唯一），如 ``"hybrid_sbd"``
    name: str

    #: 扩展点类别，与 SPI 抽象类一一对应，如 ``"memory.generation.boundary_detector"``
    category: str

    #: 语义化版本（major.minor.patch）；未来 reload 时可做版本校验
    version: str = "0.1.0"

    description: str = ""
    author: str = ""

    #: JSON Schema 字典；ConfigCenter 写入参数前据此校验
    config_schema: dict | None = None

    #: True 时 ``reload`` 不可无停机切换，必须重启进程才能生效
    requires_restart: bool = False


# ════════════════════════════════════════════════════════════════════════════
# 基类
# ════════════════════════════════════════════════════════════════════════════
class Plugin(ABC):
    """所有 SPI 实现的公共基类。

    生命周期：
        ``__init__`` → ``start(config)`` → 服务请求 → ``stop``

    配置变更触发的热更新：
        默认 ``reload(new_config)`` = ``stop`` + ``start(new_config)``
        子类可重写以实现无停机切换（如仅替换内部 LRU、不断连接）。
    """

    #: 子类必须以**类属性**形式声明（不要在 __init__ 中赋值），
    #: 否则注册表无法在 ``import`` 时通过 ``cls.meta`` 反射出来
    meta: PluginMeta

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: D401
        """在子类定义时静态校验：``meta`` 必须是 :class:`PluginMeta` 实例。

        仅对**具体**子类（已实现全部 abstractmethod）做校验；
        中间抽象类（如 :class:`memory_app.plugins.spi.boundary_detector.BoundaryDetector`）
        因尚未实现具体方法，跳过 meta 校验。
        """
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return  # 仍是抽象类
        meta = cls.__dict__.get("meta")
        if meta is not None and not isinstance(meta, PluginMeta):
            raise TypeError(
                f"{cls.__name__}.meta 必须是 PluginMeta 实例，得到 {type(meta).__name__}"
            )

    # ──────────────── 生命周期 ────────────────
    @abstractmethod
    async def start(self, config: Mapping[str, Any]) -> None:
        """启动插件：建立连接、加载模型、预热缓存。

        :param config: 已经过 schema 校验的参数字典（来自 ConfigCenter）。
                       子类可信任 config 内每个字段都符合 ``meta.config_schema``。
        """

    @abstractmethod
    async def stop(self) -> None:
        """优雅停止：释放连接、刷新缓冲、写入持久化状态。

        必须幂等 —— ``stop`` 后再次 ``stop`` 不应抛异常。
        """

    # ──────────────── 可观测 ────────────────
    async def health(self) -> dict:
        """健康检查。返回 ``{"status": "ok|degraded|fail", "detail": "..."}``。

        默认返回 ok；子类应在内部依赖不可达 / 模型未加载等情况下覆盖此方法。
        """
        return {"status": "ok"}

    async def metrics(self) -> dict:
        """运行指标：返回 Prometheus 友好的 dict（``{name: value}``）。

        默认空字典；具体实现可上报 QPS / latency / error_rate 等。
        """
        return {}

    # ──────────────── 热更新 ────────────────
    async def reload(self, new_config: Mapping[str, Any]) -> None:
        """配置热更新。

        默认实现 = ``stop`` + ``start(new_config)``，**会断开连接并重新建立**。
        如需无停机切换（如仅修改阈值、不重连 DB），子类应重写本方法。
        """
        await self.stop()
        await self.start(new_config)


__all__ = [
    "Plugin",
    "PluginMeta",
    "PluginError",
    "PluginErrorCategory",
    "PluginConflictError",
    "PluginNotFoundError",
]
