"""Plugin SPI 公共入口。

业务平面统一通过本模块取得 SPI 抽象与公共服务：

.. code-block:: python

    from memory_app.plugins import (
        Plugin, PluginMeta, PluginError,    # 公共契约
        registry, register,                  # 注册表 + 装饰器
        PluginFactory,                       # 由配置中心驱动构造插件实例
    )

铁律：
    1. 业务平面**禁止**直接 ``from memory_app.plugins_default.* import *``
    2. 任何具体实现必须以 ``@register`` 装饰 + 携带 :class:`PluginMeta`
    3. 业务管线只调 ``factory.build(category, tenant_id, user_id)`` 取实例
    4. 第三方插件通过 ``[project.entry-points."memory_app.plugins"]`` 注册
"""

from .base import (
    Plugin,
    PluginConflictError,
    PluginError,
    PluginErrorCategory,
    PluginMeta,
    PluginNotFoundError,
)
from .factory import PluginFactory
from .registry import PluginRegistry, register, registry
from .wiring import DependencyBinder

__all__ = [
    "Plugin",
    "PluginMeta",
    "PluginError",
    "PluginErrorCategory",
    "PluginConflictError",
    "PluginNotFoundError",
    "PluginRegistry",
    "registry",
    "register",
    "PluginFactory",
    "DependencyBinder",
]
