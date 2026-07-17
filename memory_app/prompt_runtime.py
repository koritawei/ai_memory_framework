"""PromptManager 单例运行时(设计文档 §2.8.4.1)。

═══════════════════════════════════════════════════════════════════════════════
契约
═══════════════════════════════════════════════════════════════════════════════
- :func:`init_prompt_manager(cc)`     由 FastAPI lifespan 在 ConfigCenter
                                      就绪后调用一次,设置全局 manager
- :func:`get_prompt_manager()`        业务平面(Phase 3+ 提取器)取 manager
                                      的**唯一**入口;未 init 时回退到
                                      :class:`StandalonePromptManager`,
                                      便于单测 / 评测脚本场景

═══════════════════════════════════════════════════════════════════════════════
为什么不放在 deps.AppState
═══════════════════════════════════════════════════════════════════════════════
PromptManager 在 lifespan 之外的代码(单元测试、CLI 脚本)也需要可达;
单独的模块级单例避免业务代码强依赖 FastAPI 的 ``app.state`` 容器。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional, Union

from memory_app.prompt_manager.config_backed import ConfigCenterPromptManager
from memory_app.prompt_manager.manager import StandalonePromptManager

logger = logging.getLogger(__name__)


PromptManagerLike = Union[ConfigCenterPromptManager, StandalonePromptManager]


_manager: PromptManagerLike | None = None
# 异步互斥:防止并发 lifespan / 测试 attach 时构造两个 ConfigCenterPromptManager
# (那会让 ConfigCenter 上挂两份 watcher,每次配置变更触发双回调)。
_init_lock: asyncio.Lock = asyncio.Lock()
# 同步互斥:get_prompt_manager 是同步函数,asyncio.Lock 用不了,但仍需防止
# 业务平面同步路径(测试 / 评测脚本)双初始化 StandalonePromptManager。
_fallback_lock: threading.Lock = threading.Lock()


async def init_prompt_manager(config_center) -> ConfigCenterPromptManager:  # noqa: ANN001
    """在 lifespan 启动期调用,绑定 ConfigCenter 创建运行时 PromptManager。

    幂等:重复调用返回同一实例(不重复 attach watcher)。
    """
    global _manager
    # fast path:已 init 同一个 cc,无需取锁
    if isinstance(_manager, ConfigCenterPromptManager) and _manager._cc is config_center:
        return _manager
    async with _init_lock:
        # 二次检查:别的协程在等锁期间已完成 init
        if isinstance(_manager, ConfigCenterPromptManager) and _manager._cc is config_center:
            return _manager
        mgr = ConfigCenterPromptManager(config_center)
        await mgr.attach_watcher()
        _manager = mgr
        logger.info(
            "prompt manager initialized (backend=%s)", type(config_center).__name__
        )
        return mgr


def get_prompt_manager() -> PromptManagerLike:
    """业务平面获取 PromptManager 的唯一入口。

    若未 :func:`init_prompt_manager`,返回 :class:`StandalonePromptManager`
    回退实现 —— 业务代码无需感知,渲染将基于内置种子工作。
    """
    global _manager
    if _manager is not None:
        return _manager
    with _fallback_lock:
        # 二次检查:别的线程在等锁期间已分配 fallback
        if _manager is not None:
            return _manager
        _manager = StandalonePromptManager()
        logger.warning(
            "prompt manager not initialized; falling back to StandalonePromptManager"
        )
        return _manager


def reset_prompt_manager_for_test() -> None:
    """重置全局 manager。**仅供测试** —— 生产绝不应调用。"""
    global _manager
    _manager = None


__all__ = [
    "PromptManagerLike",
    "init_prompt_manager",
    "get_prompt_manager",
    "reset_prompt_manager_for_test",
]
