"""ServiceBuilder —— 装配业务服务的抽象基类。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
原 ``deps.py`` 中 11 个 ``_init_<phase>_service`` 方法塞在 ``AppState`` 这一
"上帝类"内，本基类把各业务能力抽出为独立可单测的装配单元：

- 每个 builder 单文件
- 显式声明 ``name`` (日志标识) 与 ``requires`` (装配前置依赖)
- ``build(state)`` 内单一职责:从 plugin_factory 取依赖 → 创建服务 → 回写 state
- :class:`AppState.init` 简化为遍历 BUILDERS 列表,任一失败仅 warn 不阻断
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from memory_app.deps.state import AppState

logger = logging.getLogger(__name__)


class ServiceBuilder(ABC):
    """装配单个业务服务。

    子类约定:
    - 在类层声明 ``name`` 与 ``requires``(纯字符串,引用 AppState 属性名)
    - :meth:`build` 内不抛 ``ConfigCenter not ready`` 之类的前置错(由
      :meth:`can_build` 守卫);只抛真正的装配失败
    - 装配成功后写入 AppState 的对应字段
    """

    #: 日志 / metrics 标识
    name: ClassVar[str] = ""

    #: 装配前置:AppState 上必须非 None 的属性名列表
    #: 例如 ["plugin_factory", "clients.mongo_db"]
    #: dotted attr 路径会被 :meth:`can_build` 用 getattr 链解析
    requires: ClassVar[tuple[str, ...]] = ("plugin_factory",)

    @abstractmethod
    async def build(self, state: "AppState") -> None:
        """执行装配。失败由调用方(state.init)统一捕获。"""

    # ────────────────────────────────────────────────────────────────────────
    # 默认实现
    # ────────────────────────────────────────────────────────────────────────
    def can_build(self, state: "AppState") -> bool:
        """检查所有 ``requires`` 都已就绪。"""
        for attr_path in self.requires:
            obj: object | None = state
            for part in attr_path.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    return False
        return True


__all__ = ["ServiceBuilder"]
