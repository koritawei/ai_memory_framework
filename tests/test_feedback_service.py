"""FeedbackService + SynapticPlasticityReinforcer 测试(Step 5.1)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.internal_models import MemCell, MemoryState
from memory_app.plugins.spi.forgetting_policy import MemoryRef
from memory_app.plugins_default.synaptic_reinforcer import SynapticPlasticityReinforcer
from memory_app.schemas.feedback import FeedbackType
from memory_app.services import FeedbackService


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeMongoRepo:
    def __init__(self):
        self.store: dict[str, MemCell] = {}
        self.updates: list[tuple[str, dict]] = []

    async def insert(self, cell):
        self.store[cell.mem_cell_id] = cell
        return cell.mem_cell_id

    async def get_by_id(self, mid):
        return self.store.get(mid)

    async def update(self, mid, updates, **_scope):
        if mid in self.store:
            self.updates.append((mid, dict(updates)))
            cell = self.store[mid]
            for k, v in updates.items():
                if k == "state":
                    setattr(cell, "state", MemoryState(v) if isinstance(v, str) else v)
                else:
                    setattr(cell, k, v)
            return True
        return False


def _cell(strength=1.0, access_count=0) -> MemCell:
    return MemCell(
        tenant_id="t1", user_id="u1", session_id="s1",
        text="some text content", strength=strength, access_count=access_count,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


# ════════════════════════════════════════════════════════════════════════════
# SynapticPlasticityReinforcer 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestSynapticPlasticityReinforcer:
    async def test_positive_increases_strength(self):
        r = SynapticPlasticityReinforcer()
        await r.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=1.0, access_count=0,
            created_at=datetime.now(timezone.utc),
        )
        new = await r.reinforce(ref, FeedbackType.POSITIVE)
        # 1.0 + 0.3 * 0.3 = 1.09
        assert new == pytest.approx(1.09)

    async def test_negative_decreases_strength(self):
        r = SynapticPlasticityReinforcer()
        await r.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=2.0, access_count=0,
            created_at=datetime.now(timezone.utc),
        )
        new = await r.reinforce(ref, FeedbackType.NEGATIVE)
        # 2.0 + 0.3 * (-0.5) = 1.85
        assert new == pytest.approx(1.85)

    async def test_explicit_signal_overrides_default(self):
        r = SynapticPlasticityReinforcer()
        await r.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=1.0, access_count=0,
            created_at=datetime.now(timezone.utc),
        )
        new = await r.reinforce(ref, FeedbackType.POSITIVE, signal_value=2.0)
        # 1.0 + 0.3 * 2.0 = 1.6
        assert new == pytest.approx(1.6)

    async def test_clamp_to_smax(self):
        r = SynapticPlasticityReinforcer()
        await r.start({"s_max": 3.0})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=2.9, access_count=0,
            created_at=datetime.now(timezone.utc),
        )
        new = await r.reinforce(ref, FeedbackType.EXPLICIT_CONFIRM)
        # 2.9 + 0.3 * 1.0 = 3.2 → clamp 3.0
        assert new == 3.0

    async def test_clamp_to_zero(self):
        r = SynapticPlasticityReinforcer()
        await r.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=0.1, access_count=0,
            created_at=datetime.now(timezone.utc),
        )
        new = await r.reinforce(ref, FeedbackType.CORRECTION)
        # 0.1 + 0.3 * (-2.0) = -0.5 → clamp 0
        assert new == 0.0

    async def test_metrics_increment(self):
        r = SynapticPlasticityReinforcer()
        await r.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=1.0, access_count=0,
            created_at=datetime.now(timezone.utc),
        )
        await r.reinforce(ref, FeedbackType.POSITIVE)
        await r.reinforce(ref, FeedbackType.POSITIVE)
        m = await r.metrics()
        assert m["synaptic_reinforce_calls"] == 2

    async def test_explain_returns_audit_dict(self):
        r = SynapticPlasticityReinforcer()
        await r.start({})
        ref = MemoryRef(
            memory_id="m1", memory_type="EPISODIC",
            strength=1.0, access_count=0,
            created_at=datetime.now(timezone.utc),
        )
        info = r.explain(ref, FeedbackType.POSITIVE)
        assert info["old_strength"] == 1.0
        assert info["new_strength"] == pytest.approx(1.09)
        assert info["signal"] == 0.3


# ════════════════════════════════════════════════════════════════════════════
# FeedbackService
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestFeedbackService:
    async def test_positive_feedback_persists(self):
        repo = _FakeMongoRepo()
        cell = _cell()
        await repo.insert(cell)
        r = SynapticPlasticityReinforcer()
        await r.start({})
        svc = FeedbackService(mongo_repo=repo, reinforcer=r)
        result = await svc.apply_feedback(
            tenant_id="t1",
            user_id="u1",
            mem_cell_id=cell.mem_cell_id,
            memory_id=None,
            feedback_type=FeedbackType.POSITIVE,
        )
        assert result is not None
        assert result["new_strength"] > result["old_strength"]
        assert result["delta"] == pytest.approx(0.09)
        assert result["access_count"] == 1
        # 持久化已发生
        assert len(repo.updates) == 1

    async def test_negative_feedback_no_access_count_increment(self):
        repo = _FakeMongoRepo()
        cell = _cell()
        await repo.insert(cell)
        r = SynapticPlasticityReinforcer()
        await r.start({})
        svc = FeedbackService(mongo_repo=repo, reinforcer=r)
        result = await svc.apply_feedback(
            tenant_id="t1",
            user_id="u1",
            mem_cell_id=cell.mem_cell_id,
            memory_id=None,
            feedback_type=FeedbackType.NEGATIVE,
        )
        assert result["delta"] < 0
        # 负向反馈不增加 access_count
        assert result["access_count"] == 0

    async def test_explicit_signal_value(self):
        repo = _FakeMongoRepo()
        cell = _cell(strength=1.0)
        await repo.insert(cell)
        r = SynapticPlasticityReinforcer()
        await r.start({})
        svc = FeedbackService(mongo_repo=repo, reinforcer=r)
        result = await svc.apply_feedback(
            tenant_id="t1",
            user_id="u1",
            mem_cell_id=cell.mem_cell_id,
            memory_id=None,
            feedback_type=FeedbackType.POSITIVE,
            signal_value=2.0,
        )
        # 1.0 + 0.3 * 2.0 = 1.6
        assert result["new_strength"] == pytest.approx(1.6)

    async def test_not_found_returns_none(self):
        repo = _FakeMongoRepo()
        r = SynapticPlasticityReinforcer()
        await r.start({})
        svc = FeedbackService(mongo_repo=repo, reinforcer=r)
        result = await svc.apply_feedback(
            tenant_id="t1",
            user_id="u1",
            mem_cell_id="nonexistent",
            memory_id=None,
            feedback_type=FeedbackType.POSITIVE,
        )
        assert result is None

    async def test_no_id_returns_none(self):
        repo = _FakeMongoRepo()
        r = SynapticPlasticityReinforcer()
        await r.start({})
        svc = FeedbackService(mongo_repo=repo, reinforcer=r)
        result = await svc.apply_feedback(
            tenant_id="t1",
            user_id="u1",
            mem_cell_id=None,
            memory_id=None,
            feedback_type=FeedbackType.POSITIVE,
        )
        assert result is None

    async def test_explicit_confirm_strong(self):
        repo = _FakeMongoRepo()
        cell = _cell(strength=1.0)
        await repo.insert(cell)
        r = SynapticPlasticityReinforcer()
        await r.start({})
        svc = FeedbackService(mongo_repo=repo, reinforcer=r)
        result = await svc.apply_feedback(
            tenant_id="t1",
            user_id="u1",
            mem_cell_id=cell.mem_cell_id,
            memory_id=None,
            feedback_type=FeedbackType.EXPLICIT_CONFIRM,
        )
        # 1.0 + 0.3 * 1.0 = 1.3,access_count +1(positive)
        assert result["new_strength"] == pytest.approx(1.3)
        assert result["access_count"] == 1

    async def test_tenant_mismatch_returns_none(self):
        repo = _FakeMongoRepo()
        cell = _cell()
        await repo.insert(cell)
        r = SynapticPlasticityReinforcer()
        await r.start({})
        svc = FeedbackService(mongo_repo=repo, reinforcer=r)
        result = await svc.apply_feedback(
            tenant_id="other_tenant",
            user_id="u1",
            mem_cell_id=cell.mem_cell_id,
            memory_id=None,
            feedback_type=FeedbackType.POSITIVE,
        )
        assert result is None
        assert len(repo.updates) == 0

    async def test_legacy_update_path_returns_none_when_scoped_update_fails(self):
        """无 atomic 方法时,scoped update 失败不得假装成功。"""

        class _LegacyRepo(_FakeMongoRepo):
            async def update(self, mid, updates, **_scope):
                # 模拟 scoped filter 未命中
                return False

        repo = _LegacyRepo()
        cell = _cell()
        await repo.insert(cell)
        r = SynapticPlasticityReinforcer()
        await r.start({})
        svc = FeedbackService(mongo_repo=repo, reinforcer=r)
        result = await svc.apply_feedback(
            tenant_id="t1",
            user_id="u1",
            mem_cell_id=cell.mem_cell_id,
            memory_id=None,
            feedback_type=FeedbackType.POSITIVE,
        )
        assert result is None
