"""PluginRegistry —— 插件注册与发现。

═══════════════════════════════════════════════════════════════════════════════
注册表的角色
═══════════════════════════════════════════════════════════════════════════════
在「业务管线只调 SPI 抽象 + 配置中心选具体实现」的整体架构里，**注册表是
业务管线找到具体实现的唯一入口**。它做三件事：

1. **登记**：内置实现通过 ``@register`` 装饰器、第三方包通过 entry-point、
   测试代码通过 ``registry.register(cls)`` 都能登记到同一个表
2. **检索**：``registry.get(category, name)`` 给定 (category, name) 拿到类
3. **列表**：``registry.list(category)`` / ``registry.describe`` 供 Admin API
   暴露给运维

注意：注册表只存**类**，不存**实例**。实例化 + 启动由 :class:`PluginFactory`
负责，因为实例化时机依赖于 ConfigCenter 解析的参数。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from importlib.metadata import entry_points
from typing import Iterable

from .base import (
    Plugin,
    PluginConflictError,
    PluginMeta,
    PluginNotFoundError,
)

logger = logging.getLogger(__name__)


class PluginRegistry:
    """全局插件注册表，按 ``(category, name)`` 索引。

    线程安全说明：当前实现非线程安全。FastAPI 默认单事件循环 + 启动期注册，
    生产场景不会出现并发写入；如需多线程 / 多进程注册，需要外部加锁。
    """

    def __init__(self) -> None:
        # 二维 dict：``_plugins[category][name] = cls``
        self._plugins: dict[str, dict[str, type[Plugin]]] = defaultdict(dict)

    # ════════════════════════════════════════════════════════════════════════
    # 注册
    # ════════════════════════════════════════════════════════════════════════
    def register(self, cls: type[Plugin]) -> type[Plugin]:
        """以装饰器或函数形式注册一个插件类。

        :raises TypeError: ``cls`` 缺少 :class:`PluginMeta`
        :raises PluginConflictError: 同 ``(category, name)`` 已存在不同类

        幂等性：若同一个类已注册（``existing is cls``），不抛异常 —— 这处理了
        多次 ``import`` 同一模块的场景（如测试 fixture 重复调用）。
        """
        meta = getattr(cls, "meta", None)
        if not isinstance(meta, PluginMeta):
            raise TypeError(
                f"{cls.__name__} 缺少 PluginMeta，无法注册（请在类上声明 meta = PluginMeta(...)）"
            )
        bucket = self._plugins[meta.category]
        if meta.name in bucket:
            existing = bucket[meta.name]
            if existing is cls:
                # 同一个类多次 import 不应报错 —— pytest fixture / hot reload 场景
                return cls
            raise PluginConflictError(meta.category, meta.name)
        bucket[meta.name] = cls
        logger.debug("plugin registered: %s/%s -> %s", meta.category, meta.name, cls.__name__)
        return cls

    # ════════════════════════════════════════════════════════════════════════
    # 第三方包发现
    # ════════════════════════════════════════════════════════════════════════
    def discover_entry_points(self, group: str = "memory_app.plugins") -> int:
        """扫描已安装包中声明的 ``[project.entry-points."memory_app.plugins"]``。

        典型用法：第三方插件包 ``memory-plugin-qdrant`` 发布到 PyPI / 内网 PyPI，
        只要在 ``pyproject.toml`` 中声明：

        .. code-block:: toml

            [project.entry-points."memory_app.plugins"]
            qdrant_store = "memory_plugin_qdrant.store:QdrantVectorStore"

        本服务安装该包后，启动期自动调本方法发现并注册，业务无任何改动。

        :returns: 成功注册的插件数量

        加载/注册失败的条目仅 warn 不中断扫描 —— 单个第三方包损坏不应阻断启动。
        """
        loaded = 0
        try:
            eps: Iterable = entry_points(group=group)
        except TypeError:
            # Python <3.10 fallback；项目要求 3.11+ 但保留以防意外
            eps = entry_points().get(group, [])  # type: ignore[attr-defined]

        for ep in eps:
            try:
                cls = ep.load()
                if not isinstance(cls, type) or not issubclass(cls, Plugin):
                    logger.warning("entry point %s did not load a Plugin subclass", ep.name)
                    continue
                self.register(cls)
                loaded += 1
            except PluginConflictError:
                # 第三方包重复注册（如 A 包和 B 包都注册了 ``qdrant_store``）：
                # 视为配置冲突，跳过后注册的，让运维通过 logs 排查
                logger.warning("entry point %s conflicts with existing plugin; skipped", ep.name)
            except Exception as e:  # noqa: BLE001
                logger.warning("entry point %s failed to load: %s", ep.name, e)
        return loaded

    # ════════════════════════════════════════════════════════════════════════
    # 查询
    # ════════════════════════════════════════════════════════════════════════
    def get(self, category: str, name: str) -> type[Plugin]:
        """精确查找插件类。

        :raises PluginNotFoundError: 找不到（同时是 LookupError，便于通用捕获）
        """
        try:
            return self._plugins[category][name]
        except KeyError as e:
            raise PluginNotFoundError(category, name) from e

    def list(self, category: str | None = None) -> list[type[Plugin]]:
        """列出某 category 或全部已注册的插件类。"""
        if category is not None:
            return list(self._plugins.get(category, {}).values())
        return [c for cs in self._plugins.values() for c in cs.values()]

    def categories(self) -> list[str]:
        """所有出现过的 category 名（按字典序）。"""
        return sorted(self._plugins.keys())

    def describe(self) -> dict[str, list[dict]]:
        """供 Admin API 序列化返回的快照。

        返回 ``{category: [{name, version, description, author, requires_restart}, ...]}``。
        """
        out: dict[str, list[dict]] = {}
        for cat, bucket in self._plugins.items():
            out[cat] = [
                {
                    "name": cls.meta.name,
                    "version": cls.meta.version,
                    "description": cls.meta.description,
                    "author": cls.meta.author,
                    "requires_restart": cls.meta.requires_restart,
                }
                for cls in bucket.values()
            ]
        return out

    # ════════════════════════════════════════════════════════════════════════
    # 测试辅助
    # ════════════════════════════════════════════════════════════════════════
    def clear(self) -> None:
        """清空所有注册项。**仅供测试使用** —— 生产环境绝不应调用。"""
        self._plugins.clear()


#: 模块级全局单例。所有 ``@register`` 默认注册到这里。
registry = PluginRegistry()


def register(cls: type[Plugin]) -> type[Plugin]:
    """注册到全局 :data:`registry` 的便捷装饰器。

    用法：

    .. code-block:: python

        @register
        class MySBD(BoundaryDetector):
            meta = PluginMeta(name="my_sbd", category="...", version="1.0.0")
            ...
    """
    return registry.register(cls)


__all__ = ["PluginRegistry", "registry", "register"]
