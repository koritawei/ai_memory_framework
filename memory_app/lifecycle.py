"""LifecycleUpdater —— 检索命中后生命周期轻量更新(设计文档 §6.6)。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
检索完成后**异步**更新被命中 MemCell:
- ``strength``       += 0.1(截断到 ``s_max=10.0``)
- ``access_count``   += 1
- ``state``          根据访问频率 + 时间窗重算
- ``updated_at``     置为当前时刻

**设计铁律**:
- 只更新 MongoDB(SOT);ES / Milvus 由 Reconciler(Phase 6+)对齐
- fire-and-forget:经 :class:`BackgroundTaskRunner` 提交,失败入 DLQ
- 与 :class:`Reinforcer` SPI 是**互补**关系:
  - Reinforcer:用户**显式**反馈 → 大幅 +/- strength
  - LifecycleUpdater:用户**隐式**命中 → 微量 +0.1

═══════════════════════════════════════════════════════════════════════════════
状态转换矩阵(§7.1.3)
═══════════════════════════════════════════════════════════════════════════════
::

    HOT     access_count >= 5  或  age <  24h
    WARM    access_count >= 2  或  age <  7d
    COLD    access_count >= 0  且  age <  90d
    ARCHIVED 否则

注:实际 Phase 5 的 MemoryState 枚举只有 ACTIVE/WARM/COLD/ARCHIVED,
"HOT" 映射为 ACTIVE(高频活跃)。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from memory_app.internal_models import MemoryState
from memory_app.repositories.scope import tenant_scope_kwargs

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════════════
DEFAULT_STRENGTH_DELTA = 0.1
# 与 SynapticPlasticityReinforcer.meta.config_schema 的 s_max 默认值对齐 ——
# 避免隐式命中"reinforcer 裁到 5,lifecycle 推到 10"的双源裁剪矛盾。
# 操作员若改 Reinforcer 的 s_max,builder 层应同步注入到 LifecycleUpdater。
DEFAULT_S_MAX = 5.0

# RedisTaskRunner 命名 handler（跨进程可序列化）
HANDLER_BULK = "lifecycle_bulk"
HANDLER_ONE = "lifecycle_one"


def compute_state(
    *,
    access_count: int,
    created_at: datetime,
    now: datetime | None = None,
) -> MemoryState:
    """根据访问次数 + 年龄计算状态。"""
    now = now or _utcnow()
    age = _normalize(now) - _normalize(created_at)
    if access_count >= 5 or age < timedelta(hours=24):
        return MemoryState.ACTIVE  # = HOT
    if access_count >= 2 or age < timedelta(days=7):
        return MemoryState.WARM
    if age < timedelta(days=90):
        return MemoryState.COLD
    return MemoryState.ARCHIVED


# ════════════════════════════════════════════════════════════════════════════
# 主类
# ════════════════════════════════════════════════════════════════════════════
class LifecycleUpdater:
    """检索命中后异步更新生命周期字段。

    构造参数:
        ``mongo_repo``  实现 ``await get_by_id(id)`` / ``await update(id, dict)``
        ``runner``      可选 :class:`BackgroundTaskRunner`;无则用裸
                        ``asyncio.create_task``(测试场景便利)

    使用:
    .. code-block:: python

        updater = LifecycleUpdater(mongo_repo, runner)
        updater.on_retrieval_hit(["mc1", "mc2", "mc3"])  # fire-and-forget
    """

    def __init__(
        self,
        mongo_repo,
        runner=None,
        *,
        strength_delta: float = DEFAULT_STRENGTH_DELTA,
        s_max: float = DEFAULT_S_MAX,
    ) -> None:
        self._mongo_repo = mongo_repo
        self._runner = runner
        self._strength_delta = float(strength_delta)
        self._s_max = float(s_max)
        self._submitted: int = 0
        self._completed: int = 0
        # 持有 fire-and-forget 任务的强引用 —— 防止 Python GC 在任务完成前回收。
        # 见 https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
        self._inflight: set[asyncio.Task[Any]] = set()

    # ────────────────────────────────────────────────────────────────────────
    # 提交
    # ────────────────────────────────────────────────────────────────────────
    def on_retrieval_hit(
        self,
        mem_cell_ids: list[str],
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """火并忘提交。

        路径选择:
        - 若 ``mongo_repo`` 实现 ``bulk_increment_access`` → **单个**任务一次
          aggregation-pipeline update_many 完成 N 条记录,适合生产场景
          (高 QPS 下省 2N 次 Mongo round-trip)
        - 否则退化到旧的"每个 id 一个独立任务"路径 —— 便于 fake repo / 第三方
          实现兼容,以及失败重试粒度更细
        """
        if not mem_cell_ids:
            return
        self._submitted += len(mem_cell_ids)
        if hasattr(self._mongo_repo, "bulk_increment_access"):
            self._submit_bulk(list(mem_cell_ids), tenant_id=tenant_id, user_id=user_id)
            return
        # fallback:逐条单独提交
        for mid in mem_cell_ids:
            self._submit_one(mid, tenant_id=tenant_id, user_id=user_id)

    def _submit_bulk(
        self,
        mem_cell_ids: list[str],
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """提交一个 batched 任务把所有 id 一次 update_many。"""
        if self._runner is not None:
            submit_handler = getattr(self._runner, "submit_handler", None)
            if callable(submit_handler):
                submit_handler(
                    HANDLER_BULK,
                    {
                        "mem_cell_ids": list(mem_cell_ids),
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "strength_delta": self._strength_delta,
                        "s_max": self._s_max,
                    },
                    task_id=f"lifecycle:bulk:{len(mem_cell_ids)}",
                    on_failure_record={
                        "target": "lifecycle_update",
                        "operation": "bulk_update",
                    },
                )
                return

            async def _factory():
                await self._update_bulk(
                    mem_cell_ids, tenant_id=tenant_id, user_id=user_id
                )

            self._runner.submit(
                _factory,
                task_id=f"lifecycle:bulk:{len(mem_cell_ids)}",
                on_failure_record={
                    "target": "lifecycle_update",
                    "operation": "bulk_update",
                },
            )
            return
        # fallback:裸 create_task(测试 / 小规模评测)
        # 用 get_running_loop:无 running loop 时直接抛 RuntimeError,
        # 跳过任务提交即可。get_event_loop 在 Python 3.12+ 已警告、3.14 将抛错。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        task = asyncio.create_task(
            self._update_bulk(mem_cell_ids, tenant_id=tenant_id, user_id=user_id)
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _update_bulk(
        self,
        mem_cell_ids: list[str],
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """单次 Mongo update_many;失败向上抛以便 BackgroundTaskRunner 重试/DLQ。"""
        affected = await self._mongo_repo.bulk_increment_access(
            mem_cell_ids,
            strength_delta=self._strength_delta,
            s_max=self._s_max,
            **tenant_scope_kwargs(tenant_id, user_id),
        )
        self._completed += int(affected if affected is not None else len(mem_cell_ids))

    def _submit_one(
        self,
        mem_cell_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if self._runner is not None:
            submit_handler = getattr(self._runner, "submit_handler", None)
            if callable(submit_handler):
                submit_handler(
                    HANDLER_ONE,
                    {
                        "mem_cell_id": mem_cell_id,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "strength_delta": self._strength_delta,
                        "s_max": self._s_max,
                    },
                    task_id=mem_cell_id,
                    on_failure_record={
                        "target": "lifecycle_update",
                        "operation": "update",
                    },
                )
                return

            async def _factory():
                await self._update_single(
                    mem_cell_id, tenant_id=tenant_id, user_id=user_id
                )

            self._runner.submit(
                _factory,
                task_id=mem_cell_id,
                on_failure_record={
                    "target": "lifecycle_update",
                    "operation": "update",
                },
            )
            return
        # fallback:裸 create_task(只在测试 / 小规模评测使用)
        # get_running_loop 比 get_event_loop 更安全:无 loop 时直接抛错而非
        # 隐式创建新 loop(deprecation in 3.12+, error in 3.14)。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        task = asyncio.create_task(
            self._update_single(mem_cell_id, tenant_id=tenant_id, user_id=user_id)
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    # ────────────────────────────────────────────────────────────────────────
    # 同步更新单条(可被 BackgroundTaskRunner 重试)
    # ────────────────────────────────────────────────────────────────────────
    async def _update_single(
        self,
        mem_cell_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        # 优先用原子化 update_pipeline,避免 read-modify-write 并发丢失:
        # 同一 cell 两次命中都 +1,旧实现 get→inc→set 串行执行可能只生效一次。
        atomic_fn = getattr(self._mongo_repo, "atomic_apply_strength_delta", None)
        if callable(atomic_fn):
            applied = await atomic_fn(
                mem_cell_id,
                delta=self._strength_delta,
                s_max=self._s_max,
                increment_access=True,
                **tenant_scope_kwargs(tenant_id, user_id),
            )
            if applied is None:
                logger.debug("lifecycle update skipped (not found): %s", mem_cell_id)
                return None
            self._completed += 1
            return {
                "strength": float(applied["strength"]),
                "access_count": int(applied["access_count"]),
                "updated_at": _utcnow(),
            }
        # 退化路径(测试 fake repo 无原子方法):保留旧行为,但记录隐式风险
        cell = await self._mongo_repo.get_by_id(mem_cell_id)
        if cell is None:
            logger.debug("lifecycle update skipped (not found): %s", mem_cell_id)
            return None
        scope = tenant_scope_kwargs(tenant_id, user_id)
        if scope and (
            cell.tenant_id != scope["tenant_id"]
            or cell.user_id != scope["user_id"]
        ):
            logger.debug(
                "lifecycle update skipped (tenant mismatch): %s", mem_cell_id
            )
            return None
        new_strength = min(self._s_max, float(cell.strength) + self._strength_delta)
        new_access = int(cell.access_count) + 1
        new_state = compute_state(
            access_count=new_access, created_at=cell.created_at
        )
        updates = {
            "strength": new_strength,
            "access_count": new_access,
            "state": new_state.value,
            "updated_at": _utcnow(),
        }
        ok = await self._mongo_repo.update(
            mem_cell_id, updates, **tenant_scope_kwargs(tenant_id, user_id)
        )
        if not ok:
            logger.debug("lifecycle update skipped (no match): %s", mem_cell_id)
            return None
        self._completed += 1
        return updates

    # ────────────────────────────────────────────────────────────────────────
    # 同步入口(评测 / 测试)
    # ────────────────────────────────────────────────────────────────────────
    async def update_now(
        self,
        mem_cell_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._update_single(
            mem_cell_id, tenant_id=tenant_id, user_id=user_id
        )

    # ────────────────────────────────────────────────────────────────────────
    # 监控
    # ────────────────────────────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        return {"submitted": self._submitted, "completed": self._completed}


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def register_lifecycle_handlers(runner: Any, updater: "LifecycleUpdater") -> None:
    """在 RedisTaskRunner 上注册 lifecycle 命名 handler（API / worker 均需调用）。"""
    register = getattr(runner, "register_handler", None)
    if not callable(register):
        return

    async def _bulk(payload: dict[str, Any]) -> None:
        ids = list(payload.get("mem_cell_ids") or [])
        if not ids:
            return
        delta = float(payload.get("strength_delta", updater._strength_delta))
        s_max = float(payload.get("s_max", updater._s_max))
        # 临时覆盖参数，避免 builder 与队列 payload 漂移时行为不一致
        old_delta, old_max = updater._strength_delta, updater._s_max
        updater._strength_delta, updater._s_max = delta, s_max
        try:
            await updater._update_bulk(
                ids,
                tenant_id=payload.get("tenant_id"),
                user_id=payload.get("user_id"),
            )
        finally:
            updater._strength_delta, updater._s_max = old_delta, old_max

    async def _one(payload: dict[str, Any]) -> None:
        mid = payload.get("mem_cell_id") or ""
        if not mid:
            return
        delta = float(payload.get("strength_delta", updater._strength_delta))
        s_max = float(payload.get("s_max", updater._s_max))
        old_delta, old_max = updater._strength_delta, updater._s_max
        updater._strength_delta, updater._s_max = delta, s_max
        try:
            await updater._update_single(
                mid,
                tenant_id=payload.get("tenant_id"),
                user_id=payload.get("user_id"),
            )
        finally:
            updater._strength_delta, updater._s_max = old_delta, old_max

    register(HANDLER_BULK, _bulk)
    register(HANDLER_ONE, _one)


__all__ = [
    "LifecycleUpdater",
    "compute_state",
    "DEFAULT_STRENGTH_DELTA",
    "DEFAULT_S_MAX",
    "HANDLER_BULK",
    "HANDLER_ONE",
    "register_lifecycle_handlers",
]
