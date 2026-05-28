"""BasePipeline / PipelineStage 抽象。

═══════════════════════════════════════════════════════════════════════════════
为什么需要管线抽象
═══════════════════════════════════════════════════════════════════════════════
写入 / 冷路径 / 检索三条业务管线在阶段顺序、可观察性、可测试性上有共性:

1. **可插拔阶段**:新增 SignalBoost / 鉴权 / 监控阶段不应改 Service 代码
2. **统一上下文**:Stage 间通过 ``ctx`` 传递中间产物,避免 Service 内一长串临时变量
3. **可跳过**:运维通过配置临时关停某个 Stage(如关闭 ES 同步降级)
4. **可观测**:在 ``execute`` 外层统一加 metrics / tracing,无需每个 Service 重复

═══════════════════════════════════════════════════════════════════════════════
契约
═══════════════════════════════════════════════════════════════════════════════
- ``Stage.run(ctx) -> ctx``     单阶段;**约定原地修改并返回同一 ctx**(便于追踪)
- ``Pipeline.stages``          子类声明阶段顺序
- ``Pipeline.build_context``   子类把 input 包装为 ctx
- ``Pipeline.finalize(ctx)``     子类把 ctx 折叠为对外返回值
- ``Pipeline.should_skip_stage`` 子类可基于 ctx 决定是否跳过某 Stage
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 类型变量
# ════════════════════════════════════════════════════════════════════════════
CtxT = TypeVar("CtxT")
InT = TypeVar("InT")
OutT = TypeVar("OutT")


# ════════════════════════════════════════════════════════════════════════════
# 单阶段抽象
# ════════════════════════════════════════════════════════════════════════════
class PipelineStage(ABC, Generic[CtxT]):
    """管线单阶段。

    子类**只**实现 :meth:`run`;阶段间不应互相 import,所有共享状态走 ctx。
    """

    #: 阶段名(用于日志 / metrics);子类可覆盖
    name: str = ""

    @property
    def stage_name(self) -> str:
        return self.name or self.__class__.__name__

    @abstractmethod
    async def run(self, ctx: CtxT) -> CtxT:
        """执行本阶段。

        约定:
        - 应**原地修改** ctx 并返回同一对象(便于上层追踪;部分实现可返回新对象)
        - 任何阶段抛异常会中断管线;异常应是有意义的领域错误(如 ``PluginError``)
        - **禁止**在阶段内做并发 fan-out 等待(让 ``execute`` 控制时序)
        """


# ════════════════════════════════════════════════════════════════════════════
# 管线模板
# ════════════════════════════════════════════════════════════════════════════
class BasePipeline(ABC, Generic[InT, OutT, CtxT]):
    """业务管线基类(模板方法)。

    流程::

        execute(input)
          ├── build_context(input)        子类实现
          ├── for stage in stages:
          │     if should_skip_stage(stage, ctx): continue
          │     ctx = await stage.run(ctx)
          └── finalize(ctx)               子类实现 → 返回 OutT
    """

    @abstractmethod
    def stages(self) -> list[PipelineStage[CtxT]]:
        """子类声明本管线包含哪些阶段(顺序敏感)。"""

    @abstractmethod
    async def build_context(self, input_data: InT) -> CtxT:
        """把 input 包装为初始 ctx。"""

    @abstractmethod
    async def finalize(self, ctx: CtxT) -> OutT:
        """把最终 ctx 折叠为对外返回值。"""

    async def should_skip_stage(
        self, stage: PipelineStage[CtxT], ctx: CtxT
    ) -> bool:
        """允许子类按 ctx 状态跳过某 stage。默认不跳过。"""
        return False

    # ════════════════════════════════════════════════════════════════════════
    # 主入口
    # ════════════════════════════════════════════════════════════════════════
    async def execute(self, input_data: InT) -> OutT:
        """执行整条管线。"""
        ctx = await self.run_to_context(input_data)
        return await self.finalize(ctx)

    async def run_to_context(self, input_data: InT) -> CtxT:
        """执行 build_context + 所有 stages,但**跳过** ``finalize``。

        当业务门面(如 ``IngestService``)既需要落库结果又需要中间 ctx 数据
        (热路径 cells 给冷路径调度)时,直接调本方法避免重复 ``execute`` 流程。
        统一在这里加 logging / metrics / 错误处理,任何子类都不需要重新实现。
        """
        ctx = await self.build_context(input_data)
        for stage in self.stages():
            if await self.should_skip_stage(stage, ctx):
                logger.debug("pipeline skipping stage: %s", stage.stage_name)
                continue
            try:
                ctx = await stage.run(ctx)
            except Exception as e:
                logger.warning(
                    "pipeline stage %s failed: %s", stage.stage_name, e
                )
                raise
        return ctx


__all__ = ["BasePipeline", "PipelineStage", "CtxT", "InT", "OutT"]
