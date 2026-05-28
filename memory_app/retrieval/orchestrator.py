"""RetrievalOrchestrator —— 检索管线对外门面。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
- :class:`RetrievalPipeline` 的别名 + 显式 ``retrieve`` 方法,便于 Router 层调
- 路由层 ``Depends(get_retrieval_orchestrator)`` 注入实例
- 提供 :meth:`add_finalize_hook` 公开 API,供下游 builder(如 反馈与生命周期
  LifecycleUpdater)在 finalize 后插入额外逻辑,**替代旧版 monkey-patch**
  ``orch.finalize = wrapped_finalize`` 反模式

业务平面只需:

.. code-block:: python

    ranked = await orchestrator.retrieve(request)
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from memory_app.internal_models import RankedMemory
from memory_app.pipelines.retrieval import RetrievalPipeline, RetrievalPipelineContext
from memory_app.schemas.retrieve import RetrieveMemRequest

logger = logging.getLogger(__name__)

#: finalize hook 签名:``async (results: list[RankedMemory]) -> None``。
#: 异常被本类吞掉并 warn(单个钩子失败不应破坏检索本体)。
FinalizeHook = Callable[[list[RankedMemory]], Awaitable[None]]


class RetrievalOrchestrator(RetrievalPipeline):
    """与 :class:`RetrievalPipeline` 等价,提供更直观的方法名 + finalize 钩子。"""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._finalize_hooks: list[FinalizeHook] = []

    def add_finalize_hook(self, hook: FinalizeHook) -> None:
        """注册一个在 :meth:`finalize` 之后异步执行的钩子。

        典型用法:反馈与生命周期 ``LifecycleUpdater.on_retrieval_hit`` 由
        ``FeedbackLifecycleBuilder`` 在装配末尾注册,无需 monkey-patch。

        钩子按注册顺序串行调用,**单个钩子抛异常仅记 warn 不打断后续钩子**。
        """
        self._finalize_hooks.append(hook)

    def add_recall_channel(self, name: str, channel) -> bool:  # type: ignore[no-untyped-def]
        """对外开放的"追加 recall 通道"API,替代 builder 直读 ``self._recall``。

        图与实体 GraphComponentsBuilder 在 entity / graph 通道就绪后调本方法
        把通道挂上,以前是 ``getattr(orch, "_recall", None).add_channel(...)``
        —— 重构 ``RetrievalPipeline`` 内部布局时会静默断掉。

        :returns: 是否成功挂入(True);若内部没有 RecallStage 则返回 False
        """
        from memory_app.pipelines.retrieval import RecallStage
        # _recall 是 RetrievalPipeline 当前实现细节,只在本类内部访问,
        # 不让 builder 跨进来。
        recall = getattr(self, "_recall", None)
        if not isinstance(recall, RecallStage):
            return False
        recall.add_channel(name, channel)
        return True

    async def finalize(
        self, ctx: RetrievalPipelineContext
    ) -> list[RankedMemory]:
        results = await super().finalize(ctx)
        for hook in self._finalize_hooks:
            try:
                await hook(results)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "retrieval finalize hook %s failed: %s",
                    getattr(hook, "__qualname__", repr(hook)), e,
                )
        return results

    async def retrieve(self, request: RetrieveMemRequest) -> list[RankedMemory]:
        return await self.execute(request)


__all__ = ["RetrievalOrchestrator", "FinalizeHook"]
