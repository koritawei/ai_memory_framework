"""Demo: Phase 5 反馈生命周期(``POST /v1/memory/feedback``)。

═══════════════════════════════════════════════════════════════════════════════
本 demo 走读
═══════════════════════════════════════════════════════════════════════════════
反馈分两类:

- **显式反馈**(用户点赞/纠正)→ ``POST /v1/memory/feedback`` → 调 ``FeedbackService``
- **隐式反馈**(检索命中后 strength +0.1)→ ``LifecycleUpdater.on_retrieval_hit``

两者都走"读旧 strength → 算新 strength → 落 Mongo"流程,关键不变量:

1. ``Reinforcer.reinforce()`` 算新强度,但**不**写入(写入由 Service 调
   ``MongoRepo.atomic_apply_strength_delta`` 完成)
2. ``atomic_apply_strength_delta`` 走 Mongo aggregation pipeline,**服务端**
   做 ``$min/$add`` —— 同一 cell 的并发反馈不会丢失更新
3. POSITIVE / EXPLICIT_CONFIRM 才递增 access_count;NEGATIVE / CORRECTION 不递增

::

  POST /v1/memory/feedback {type=POSITIVE, signal_value=0}
       │
       ▼
  FeedbackService.apply_feedback
       │  ├─ mongo_repo.get_by_id  → 读旧 cell
       │  ├─ Reinforcer.reinforce  → 算新 strength
       │  └─ mongo_repo.atomic_apply_strength_delta  → 原子化写入
       ▼
  {"old_strength", "new_strength", "delta", "access_count"}

本 demo 重点演示并发场景:同一 cell 的 3 个并发 POSITIVE 反馈,
最终 strength 必须是 ``min(s_max, old + 3×Δ)`` —— 原子化保证不丢更新。
"""

from __future__ import annotations

import asyncio

import pytest

from memory_app.internal_models import MemoryState
from memory_app.plugins_default.synaptic_reinforcer import (
    SynapticPlasticityReinforcer,
)
from memory_app.schemas.feedback import FeedbackType
from memory_app.services import FeedbackService

from .conftest import make_cell


# ════════════════════════════════════════════════════════════════════════════
# 共用:构造 Reinforcer 实例(start 一次,wire 进 service)
# ════════════════════════════════════════════════════════════════════════════
async def _build_reinforcer() -> SynapticPlasticityReinforcer:
    r = SynapticPlasticityReinforcer()
    await r.start({})  # 跑默认 schema:eta=0.3, lambda=0.01, s_max=5.0
    return r


# ════════════════════════════════════════════════════════════════════════════
# 1. 单次 POSITIVE 反馈 —— 走通完整流程
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_positive_feedback_increases_strength_and_access(fake_mongo):
    """POSITIVE 反馈 signal_value=0(让服务端按 type 表填默认 +0.3),
    应该 strength += 0.3*0.3 ≈ 0.09,access_count += 1。"""
    cell = make_cell(text="原始记忆", strength=2.0, access_count=3)
    await fake_mongo.insert(cell)

    reinforcer = await _build_reinforcer()
    service = FeedbackService(mongo_repo=fake_mongo, reinforcer=reinforcer)

    result = await service.apply_feedback(
        tenant_id=cell.tenant_id,
        user_id=cell.user_id,
        mem_cell_id=cell.mem_cell_id,
        memory_id=None,
        feedback_type=FeedbackType.POSITIVE,
        signal_value=0.0,  # 服务端按表填默认 +0.3
    )

    assert result is not None, "返回 None 表示目标记忆不存在(404)"
    assert result["mem_cell_id"] == cell.mem_cell_id
    assert result["old_strength"] == pytest.approx(2.0)
    # POSITIVE 默认 signal=+0.3, eta=0.3 → delta = 0.3*0.3 = 0.09
    assert result["delta"] == pytest.approx(0.09, abs=1e-3)
    assert result["new_strength"] == pytest.approx(2.09, abs=1e-3)
    # POSITIVE 视为正向 → access_count++
    assert result["access_count"] == 4

    # ── 原子化路径被调到 ─────────────────────────────────────────────────
    assert len(fake_mongo.atomic_calls) == 1
    mid, delta, s_max, inc_access = fake_mongo.atomic_calls[0]
    assert mid == cell.mem_cell_id
    assert delta == pytest.approx(0.09, abs=1e-3)
    # s_max 来自 Reinforcer.config.s_max(默认 5.0),由 _resolve_reinforcer_s_max 提取
    assert s_max == pytest.approx(5.0)
    assert inc_access is True

    # 落库后的 cell 也确实更新了
    stored = await fake_mongo.get_by_id(cell.mem_cell_id)
    assert stored.strength == pytest.approx(2.09, abs=1e-3)
    assert stored.access_count == 4


# ════════════════════════════════════════════════════════════════════════════
# 2. NEGATIVE 反馈 —— strength 下降,access_count 不变
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_negative_feedback_decreases_strength_but_not_access(fake_mongo):
    """NEGATIVE 默认 signal=-0.5 → delta = 0.3*(-0.5) = -0.15。
    access_count 不变(只有正向反馈才"+1 访问")。"""
    cell = make_cell(text="差评记忆", strength=2.0, access_count=5)
    await fake_mongo.insert(cell)

    service = FeedbackService(
        mongo_repo=fake_mongo, reinforcer=await _build_reinforcer()
    )
    result = await service.apply_feedback(
        tenant_id=cell.tenant_id,
        user_id=cell.user_id,
        mem_cell_id=cell.mem_cell_id,
        memory_id=None,
        feedback_type=FeedbackType.NEGATIVE,
        signal_value=0.0,
    )

    assert result["delta"] == pytest.approx(-0.15, abs=1e-3)
    assert result["new_strength"] == pytest.approx(1.85, abs=1e-3)
    # 关键不变量:NEGATIVE 不递增 access
    assert result["access_count"] == 5
    # 原子化调用的 increment_access 应为 False
    _, _, _, inc_access = fake_mongo.atomic_calls[0]
    assert inc_access is False


# ════════════════════════════════════════════════════════════════════════════
# 3. 并发反馈不丢失更新 —— atomic_apply_strength_delta 的存在意义
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_concurrent_positive_feedbacks_no_lost_update(fake_mongo):
    """3 个并发 POSITIVE 反馈到同一 cell,落库 strength 必须等于 ``old + 3×Δ``。

    旧实现(read-modify-write)在并发时会丢更新:三次都读到 strength=2.0,
    各自计算 new=2.09,最终落库还是 2.09(丢了两次 +0.09)。
    新实现走 ``atomic_apply_strength_delta``,Mongo 服务端做 ``$set + $add``,
    并发安全。
    """
    cell = make_cell(text="并发反馈目标", strength=2.0, access_count=0)
    await fake_mongo.insert(cell)
    service = FeedbackService(
        mongo_repo=fake_mongo, reinforcer=await _build_reinforcer()
    )

    # 并发触发 3 个 POSITIVE 反馈
    async def _send():
        return await service.apply_feedback(
            tenant_id=cell.tenant_id,
            user_id=cell.user_id,
            mem_cell_id=cell.mem_cell_id,
            memory_id=None,
            feedback_type=FeedbackType.POSITIVE,
            signal_value=0.0,
        )

    results = await asyncio.gather(_send(), _send(), _send())

    # 3 个调用都返回成功
    assert all(r is not None for r in results)

    # 最终 stored strength = 2.0 + 3 × 0.09 = 2.27
    stored = await fake_mongo.get_by_id(cell.mem_cell_id)
    assert stored.strength == pytest.approx(2.27, abs=1e-3)
    # access_count 也被 +3
    assert stored.access_count == 3

    # 3 次原子调用都被记下
    assert len(fake_mongo.atomic_calls) == 3
    # update() 那条不被走(走原子路径)
    assert fake_mongo.updates == []


# ════════════════════════════════════════════════════════════════════════════
# 4. s_max 上限:并发反馈不会突破 5.0
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_strength_clipped_at_s_max(fake_mongo):
    """20 个并发 EXPLICIT_CONFIRM(强反馈,signal=+1)累计 delta = 20*0.3 = 6.0,
    但 strength 必须裁剪到 s_max=5.0。这是 Reinforcer 合同的核心承诺。"""
    cell = make_cell(text="高强度记忆", strength=4.5, access_count=0)
    await fake_mongo.insert(cell)
    service = FeedbackService(
        mongo_repo=fake_mongo, reinforcer=await _build_reinforcer()
    )

    async def _confirm():
        return await service.apply_feedback(
            tenant_id=cell.tenant_id,
            user_id=cell.user_id,
            mem_cell_id=cell.mem_cell_id,
            memory_id=None,
            feedback_type=FeedbackType.EXPLICIT_CONFIRM,
            signal_value=0.0,
        )

    await asyncio.gather(*(_confirm() for _ in range(20)))
    stored = await fake_mongo.get_by_id(cell.mem_cell_id)
    # 必须严格 ≤ s_max
    assert stored.strength <= 5.0 + 1e-9
    assert stored.strength == pytest.approx(5.0)
    assert stored.access_count == 20


# ════════════════════════════════════════════════════════════════════════════
# 5. 404:目标记忆不存在
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_feedback_returns_none_for_unknown_id(fake_mongo):
    """``apply_feedback`` 返回 None → 路由层映射为 HTTP 404。"""
    service = FeedbackService(
        mongo_repo=fake_mongo, reinforcer=await _build_reinforcer()
    )
    result = await service.apply_feedback(
        tenant_id="t1",
        user_id="u1",
        mem_cell_id="does-not-exist",
        memory_id=None,
        feedback_type=FeedbackType.POSITIVE,
    )
    assert result is None
    # 没有写入也没有 atomic 调用
    assert fake_mongo.atomic_calls == []
    assert fake_mongo.updates == []


# ════════════════════════════════════════════════════════════════════════════
# 6. 回退:repo 缺 atomic_apply_strength_delta 时走旧 update 路径
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_feedback_falls_back_to_update_when_atomic_unavailable():
    """模拟一个**只**实现基本 ``get_by_id`` + ``update`` 的 repo(老版本 / 第三方实现)。

    FeedbackService 应优雅降级,但**注意**:此时并发反馈会有丢更新风险。
    Demo 用单次反馈展示退化路径仍能工作,业务平面不被强依赖卡死。
    """
    class _LegacyRepo:
        """只实现最小接口的旧 repo —— 没有 atomic_apply_strength_delta。"""
        def __init__(self) -> None:
            self.store: dict = {}
            self.updates: list = []

        async def insert(self, cell):
            self.store[cell.mem_cell_id] = cell
            return cell.mem_cell_id

        async def get_by_id(self, mid):
            return self.store.get(mid)

        async def update(self, mid, updates, **_scope):
            self.updates.append((mid, dict(updates)))
            cell = self.store.get(mid)
            if cell is None:
                return False
            for k, v in updates.items():
                if k == "state" and isinstance(v, str):
                    try:
                        v = MemoryState(v)
                    except ValueError:
                        pass
                try:
                    setattr(cell, k, v)
                except Exception:
                    pass
            return True

    legacy = _LegacyRepo()
    await legacy.insert(make_cell(text="legacy", strength=1.0, access_count=0))
    target_id = next(iter(legacy.store))

    service = FeedbackService(
        mongo_repo=legacy, reinforcer=await _build_reinforcer()
    )
    result = await service.apply_feedback(
        tenant_id="t1",
        user_id="u1",
        mem_cell_id=target_id,
        memory_id=None,
        feedback_type=FeedbackType.POSITIVE,
        signal_value=0.0,
    )
    assert result is not None
    assert result["new_strength"] == pytest.approx(1.09, abs=1e-3)
    # 走的是 update 路径,记录一条
    assert len(legacy.updates) == 1
    _, payload = legacy.updates[0]
    assert payload["strength"] == pytest.approx(1.09, abs=1e-3)
    assert payload["access_count"] == 1
