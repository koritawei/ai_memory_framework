"""业务服务门面层(设计文档 §5.1 / §2.7.6)。

═══════════════════════════════════════════════════════════════════════════════
分层定位
═══════════════════════════════════════════════════════════════════════════════
- **路由层** ``routers/memory.py``    HTTP 入口,只做协议解析与响应序列化
- **服务层** ``services.py``          业务门面(本模块),委托管线;不展开阶段细节
- **管线层** ``pipelines/``           阶段链编排(§2.7.6)
- **数据层** ``repositories/``        各存储后端 CRUD

═══════════════════════════════════════════════════════════════════════════════
铁律(设计文档 Phase 2 引语)
═══════════════════════════════════════════════════════════════════════════════
**业务平面禁止**直接 ``from memory_app.plugins_default.* import *``;
SBD / 各种 Channel 等组件**必须**通过 :meth:`PluginFactory.build` 取得。

应用入口(``deps.py`` 的 ``get_ingest_service``)负责装配:
1. 调 ``factory.build("memory.generation.boundary_detector", tenant_id)`` 拿到 SBD
2. 创建 :class:`MongoMemCellRepo` (Phase 2.3 起还含 ES / Milvus repo)
3. 注入 :class:`IngestPipeline`
4. 把 pipeline 注入 :class:`IngestService`
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from memory_app.internal_models import MemCell, RawData
from memory_app.pipelines import ColdPathPipeline, IngestPipeline
from memory_app.plugins.spi.forgetting_policy import MemoryRef
from memory_app.schemas.feedback import FeedbackType

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# IngestService —— 写入门面
# ════════════════════════════════════════════════════════════════════════════
class IngestService:
    """写入热路径业务门面。

    职责:
    - 接收已转好的 :class:`RawData` 列表
    - 委托 :class:`IngestPipeline.execute`
    - 返回 ``mem_cell_id`` 列表
    - **可选**触发冷路径(Phase 3):构造时注入 ``cold_path_service`` 后,
      热路径成功落 SOT 后自动 ``schedule`` 每个 cell 的后台抽取
    """

    def __init__(
        self,
        pipeline: IngestPipeline,
        cold_path_service: "ColdPathService | None" = None,
    ) -> None:
        self._pipeline = pipeline
        self._cold_path = cold_path_service

    # ────────────────────────────────────────────────────────────────────────
    # 公开钩子(替代旧版"装配层 monkey-patch private _cold_path"反模式)
    # ────────────────────────────────────────────────────────────────────────
    def attach_cold_path(self, cold_path_service: "ColdPathService | None") -> None:
        """装配冷路径服务(Phase 3 ColdPathService 装配完成后由 builder 调用)。

        允许传 ``None`` 表示「解绑」,便于评测 / 测试场景。
        """
        self._cold_path = cold_path_service

    def sync_index_repos(self):
        """返回热路径 SyncIndexStage 绑定的 ES / Milvus 仓储。"""
        return self._pipeline.sync_index_repos()

    async def ingest(self, raw_data_list: list[RawData]) -> list[str]:
        """执行写入。

        :param raw_data_list: 已经过 ``format_transfer`` 的内部 RawData 列表
        :returns: 落库成功的 ``mem_cell_id`` 列表(顺序与 segment 一致)
        :raises Exception: 任意阶段抛出的领域错误(MongoDB 写失败等)
        """
        if not raw_data_list:
            return []
        # 复用 BasePipeline.run_to_context 模板方法 ——
        # 不再手抄 build_context + for stage 循环,跟随基类未来的
        # tracing / 错误处理增强自动起效。
        ctx = await self._pipeline.run_to_context(raw_data_list)
        cell_ids = [c.mem_cell_id for c in ctx.cells]
        # 冷路径:fire-and-forget,异常不影响热路径返回
        if self._cold_path is not None and ctx.cells:
            try:
                self._cold_path.schedule_many(ctx.cells)
            except Exception as e:  # noqa: BLE001
                logger.warning("cold path schedule failed (degraded): %s", e)
        return cell_ids


# ════════════════════════════════════════════════════════════════════════════
# ColdPathService —— 异步冷路径门面(Phase 3)
# ════════════════════════════════════════════════════════════════════════════
class ColdPathService:
    """异步冷路径业务门面(Step 3.1~3.4 的串联点)。

    职责:
    - :meth:`schedule(cell)`        把单条 MemCell 的冷路径任务提交后台运行
    - :meth:`schedule_many(cells)`  批量便利
    - :meth:`run_now(cell)`         同步串行执行(评测 / e2e 测试)

    与 IngestService 的协作:
    - IngestService.ingest 完成 MongoDB 落 SOT 后,对每条 cell 调
      :meth:`schedule` —— 不阻塞 HTTP 响应
    - 失败由 BackgroundTaskRunner 内置的重试 + DLQ 兜底
    """

    def __init__(
        self,
        pipeline: ColdPathPipeline,
        runner: "BackgroundTaskRunnerLike | None" = None,
        *,
        on_complete: "Optional[Any]" = None,
    ) -> None:
        self._pipeline = pipeline
        self._runner = runner
        # on_complete:可选 callback,签名 ``async (ctx) -> None`` —— 用于 Phase 4 把
        # ctx.episodes / ctx.semantics 落库;Phase 3 无持久化时留空
        self._on_complete = on_complete

    def schedule(self, cell: MemCell) -> None:
        """火并忘提交 cell 的冷路径。"""
        if self._runner is None:
            raise RuntimeError("ColdPathService.runner not configured")

        submit_handler = getattr(self._runner, "submit_handler", None)
        if callable(submit_handler):
            submit_handler(
                "cold_path",
                {
                    "mem_cell_id": cell.mem_cell_id,
                    "tenant_id": cell.tenant_id,
                    "user_id": cell.user_id,
                },
                task_id=cell.mem_cell_id,
                on_failure_record={"target": "cold_path", "operation": "execute"},
            )
            return

        async def _factory() -> None:
            ctx = await self._pipeline.execute(cell)
            if self._on_complete is not None:
                await self._on_complete(ctx)

        self._runner.submit(
            _factory,
            task_id=cell.mem_cell_id,
            on_failure_record={"target": "cold_path", "operation": "execute"},
        )

    def schedule_many(self, cells: list[MemCell]) -> None:
        for c in cells:
            self.schedule(c)

    async def run_now(self, cell: MemCell):
        """同步执行(便于评测 / e2e)。返回 :class:`ColdPathContext`。"""
        ctx = await self._pipeline.execute(cell)
        if self._on_complete is not None:
            await self._on_complete(ctx)
        return ctx

    def attach_stage(self, stage) -> None:  # type: ignore[no-untyped-def]
        """对外开放的"追加 cold-path stage"API。

        替代 ``service._pipeline._extra_stages.append(stage)`` 反模式,
        让 ``ColdPathPipeline`` 的内部布局变更不再要求修改 builder 代码。

        如果传入的 pipeline 是第三方子类没有 ``add_extra_stage`` 方法,
        优雅降级到 logger 警告而不是 AttributeError —— 与 builder 的防御式
        ``getattr(service, "attach_stage", None)`` 风格对齐。
        """
        adder = getattr(self._pipeline, "add_extra_stage", None)
        if not callable(adder):
            logger.warning(
                "ColdPathPipeline %s has no add_extra_stage; skip",
                type(self._pipeline).__name__,
            )
            return
        adder(stage)

    def find_extra_stage(self, predicate):  # type: ignore[no-untyped-def]
        """按谓词查询已注册的 extra stage(供装配幂等检查)。"""
        finder = getattr(self._pipeline, "find_extra_stage", None)
        if not callable(finder):
            return None
        return finder(predicate)


# ════════════════════════════════════════════════════════════════════════════
# 鸭子类型协议(避免循环 import)
# ════════════════════════════════════════════════════════════════════════════
class BackgroundTaskRunnerLike:
    def submit(self, coro_factory, *, task_id: str | None = None, on_failure_record=None): ...  # pragma: no cover


# ════════════════════════════════════════════════════════════════════════════
# FeedbackService —— 反馈门面(Phase 5 Step 5.1)
# ════════════════════════════════════════════════════════════════════════════
class FeedbackService:
    """显式 / 隐式反馈处理(设计文档 §7.5)。

    职责:
    - 取出目标 MemCell(``mem_cell_id`` / ``memory_id``)
    - 委托 :class:`Reinforcer` SPI 计算新 strength
    - 持久化到 MongoDB(SOT);ES / Milvus 的 ``access_count`` 等字段同步
      由 Phase 6+ 的 Reconciler 异步对齐(本服务不做)
    - 返回审计 dict(``old_strength`` / ``new_strength`` / ``delta`` 等)

    与 SPI 的契约:
    - ``Reinforcer.reinforce(memory_ref, feedback_type, signal_value)``
      只返回新 strength,**不**做持久化(本服务负责)
    """

    def __init__(
        self,
        mongo_repo,
        reinforcer,
    ) -> None:
        self._mongo_repo = mongo_repo
        self._reinforcer = reinforcer

    async def apply_feedback(
        self,
        *,
        tenant_id: str,
        user_id: str,
        mem_cell_id: str | None,
        memory_id: str | None,
        feedback_type: "FeedbackType",
        signal_value: float = 0.0,
        comment: str | None = None,
        retrieval_id: str | None = None,
    ) -> dict | None:
        """应用反馈;``None`` 表示目标记忆不存在或租户不匹配(对应 404)。"""
        target_id = mem_cell_id or memory_id
        if not target_id:
            return None
        scoped_get = getattr(self._mongo_repo, "get_by_id_scoped", None)
        if callable(scoped_get):
            cell = await scoped_get(
                target_id, tenant_id=tenant_id, user_id=user_id
            )
        else:
            cell = await self._mongo_repo.get_by_id(target_id)
            if cell is not None and (
                cell.tenant_id != tenant_id or cell.user_id != user_id
            ):
                logger.warning(
                    "feedback rejected: tenant/user mismatch for %s "
                    "(request=%s/%s cell=%s/%s)",
                    target_id,
                    tenant_id,
                    user_id,
                    cell.tenant_id,
                    cell.user_id,
                )
                return None
        if cell is None:
            return None

        # 快照旧值 —— 避免 _FakeMongoRepo / 实际持久化层后续就地修改 cell 实例
        old_strength = float(cell.strength)
        old_access = int(cell.access_count)

        ref = MemoryRef(
            memory_id=cell.mem_cell_id,
            memory_type="EPISODIC",  # MemCell 在 Phase 5 视为情景前驱
            state=cell.state,
            strength=old_strength,
            access_count=old_access,
            importance_score=float(cell.importance_score),
            created_at=cell.created_at,
            last_recalled_at=cell.updated_at,
        )

        # 调 SPI 取新 strength
        try:
            new_strength = await self._reinforcer.reinforce(
                ref, feedback_type, signal_value
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "reinforce failed for %s (type=%s): %s",
                target_id, feedback_type.value if hasattr(feedback_type, 'value') else feedback_type, e,
            )
            raise

        delta = float(new_strength) - old_strength
        # access_count:正向反馈 +1,负向反馈不计
        is_positive = feedback_type in (FeedbackType.POSITIVE, FeedbackType.EXPLICIT_CONFIRM)
        # 原子化:让 Mongo 服务端按 delta 累加 + 裁剪 ——
        # 并发同 cell 两次反馈不再因 Python 端 read-modify-write 而丢失更新。
        # s_max 必须与当前 Reinforcer 一致 —— 否则 Reinforcer 把 new_strength
        # 裁到 5.0,而 Mongo 服务端用 10.0 重算后存到 7+,违反 SPI 契约。
        # 若 repo 缺这个原子方法(测试 fake),回退到旧的两段式更新。
        s_max = _resolve_reinforcer_s_max(self._reinforcer)
        atomic_fn = getattr(self._mongo_repo, "atomic_apply_strength_delta", None)
        if callable(atomic_fn):
            applied = await atomic_fn(
                target_id,
                delta=delta,
                s_max=s_max,
                increment_access=is_positive,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if applied is None:
                return None
            new_access = int(applied["access_count"])
            persisted_strength = float(applied["strength"])
        else:
            new_access = old_access + (1 if is_positive else 0)
            await self._mongo_repo.update(
                target_id,
                {
                    "strength": float(new_strength),
                    "access_count": new_access,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            persisted_strength = float(new_strength)
        return {
            "mem_cell_id": target_id,
            "feedback_type": feedback_type.value if hasattr(feedback_type, 'value') else str(feedback_type),
            "old_strength": old_strength,
            "new_strength": persisted_strength,
            "delta": persisted_strength - old_strength,
            "access_count": new_access,
            "retrieval_id": retrieval_id,
            "comment": comment,
        }


def _resolve_reinforcer_s_max(reinforcer: Any) -> float:
    """从 Reinforcer 实例上提取 s_max,匹配其 strength 裁剪上限。

    支持三种暴露形式:
    - 直接属性 ``reinforcer.s_max``
    - 嵌套配置 ``reinforcer._config.s_max``(``SynapticPlasticityReinforcer`` 的形态)
    - 元数据兜底 ``meta.config_schema.properties.s_max.default``
    无法确定时回退到 5.0(与 Phase 5 默认实现保持一致),宁愿略保守也不放大上限。
    """
    direct = getattr(reinforcer, "s_max", None)
    if isinstance(direct, (int, float)) and direct > 0:
        return float(direct)
    cfg = getattr(reinforcer, "_config", None)
    if cfg is not None:
        cfg_s_max = getattr(cfg, "s_max", None)
        if isinstance(cfg_s_max, (int, float)) and cfg_s_max > 0:
            return float(cfg_s_max)
    meta = getattr(reinforcer, "meta", None)
    if meta is not None:
        schema = getattr(meta, "config_schema", {}) or {}
        try:
            default = schema["properties"]["s_max"]["default"]
            if isinstance(default, (int, float)) and default > 0:
                return float(default)
        except (KeyError, TypeError):
            pass
    return 5.0


# ════════════════════════════════════════════════════════════════════════════
# ConsolidationService —— 离线巩固门面(Phase 6 Step 6.4)
# ════════════════════════════════════════════════════════════════════════════
class ConsolidationService:
    """:class:`POST /v1/memory/consolidate` 的业务门面。

    委托 :class:`ConsolidationStrategy` SPI(默认 ``three_phase``)
    + 持久化 SemanticMemory 的简单写入(可选;Phase 6 仅记 metrics,
    持久化在 Phase 7 GraphStore 启用后由 Reconciler 接管)。
    """

    def __init__(
        self,
        strategy,
        *,
        scope_provider=None,
    ) -> None:
        self._strategy = strategy
        # 可选:由调用方提供"扫描哪些 (tenant, user)"的策略
        self._scope_provider = scope_provider
        # 串行化 set_scope_provider + run:strategy 是 PluginFactory 单例缓存的
        # 共享实例,scope_provider 写到 strategy 后再 await run() —— 两个并发
        # consolidate(tenant=A) 与 consolidate(tenant=B) 会互踩 scope,
        # 造成跨租户污染。这把锁确保"绑 scope → 跑 run → 解绑"是原子序列。
        self._call_lock: asyncio.Lock = asyncio.Lock()

    async def consolidate(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        scope: str = "all",
        dry_run: bool = False,
        time=None,
    ) -> dict:
        """触发一次巩固。

        - ``user_id=None`` 表示对该 tenant 下所有 user 扫描(由 ``scope_provider``)
        - ``dry_run`` 暂不强制约束 strategy(子策略各自实现是否真持久化);
          Phase 6 ``three_phase`` 实现内部使用 ``DecayManager.dry_run=False``
          的语义;后续可下传 dry_run 给子组件

        并发模型:同一 service 实例上的多个 consolidate 调用会被 ``self._call_lock``
        串行化,以防 set_scope_provider 的 side-effect 被另一并发调用覆盖,
        导致策略读到错误租户的 scope。
        """
        # 临时把 scope 注入 strategy(若它暴露 set_scope_provider 钩子)。
        # 历史上是直接 mutate `self._strategy._scope_provider`(private 字段),
        # 改为约定:任何想支持「按 tenant/user 缩窗」的 strategy 实现可选实现
        # `set_scope_provider(async () -> list[ConsolidationScope])` 公开方法。
        async with self._call_lock:
            if self._scope_provider is not None and tenant_id is not None:
                async def _scope() -> list:
                    return await self._scope_provider(tenant_id, user_id)

                set_scope = getattr(self._strategy, "set_scope_provider", None)
                if callable(set_scope):
                    set_scope(_scope)

            report = await self._strategy.run(scope=scope, time=time)  # type: ignore[arg-type]
        return {
            "phase": report.phase,
            "started_at": report.started_at.isoformat(),
            "finished_at": report.finished_at.isoformat(),
            "scanned_count": report.scanned_count,
            "consolidated_count": report.consolidated_count,
            "archived_count": report.archived_count,
            "forgotten_count": report.forgotten_count,
            "error_count": report.error_count,
            "notes": list(report.notes),
            "dry_run": dry_run,
        }


__all__ = [
    "IngestService",
    "ColdPathService",
    "FeedbackService",
    "ConsolidationService",
]
