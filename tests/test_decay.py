"""DecayManager + GreedyCapacityOptimizer 测试(Step 6.3)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.consolidation.decay import (
    DecayConfig,
    DecayManager,
    parse_decay_config,
)
from memory_app.internal_models import MemCell, MemoryState
from memory_app.plugins.spi.forgetting_policy import MemoryRef
from memory_app.plugins_default.greedy_capacity_optimizer import (
    GreedyCapacityOptimizer,
)
from memory_app.scoring import FSFMScorer


def _now() -> datetime:
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def _cell(
    *,
    state=MemoryState.ACTIVE,
    text="abc",
    strength=1.0,
    access_count=0,
    days_ago=0.0,
) -> MemCell:
    return MemCell(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        text=text,
        state=state,
        strength=strength,
        access_count=access_count,
        created_at=_now() - timedelta(days=days_ago),
    )


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeMongoRepo:
    """模拟 ``find_by_state`` / ``count`` / ``find_all`` / ``update``。"""

    def __init__(self):
        self.cells: dict[str, MemCell] = {}
        self.updates: list[tuple[str, dict]] = []

    async def find_by_state(self, tenant_id, user_id, state, limit=1000):
        target = state.value if hasattr(state, "value") else str(state)
        return [
            c for c in self.cells.values()
            if c.tenant_id == tenant_id and c.user_id == user_id
            and (c.state.value if hasattr(c.state, "value") else c.state) == target
        ][:limit]

    async def find_all(self, tenant_id, user_id, limit=10000):
        return [
            c for c in self.cells.values()
            if c.tenant_id == tenant_id and c.user_id == user_id
        ][:limit]

    async def count(self, tenant_id, user_id):
        return sum(
            1 for c in self.cells.values()
            if c.tenant_id == tenant_id and c.user_id == user_id
        )

    async def update(self, mid, updates):
        if mid not in self.cells:
            return False
        self.updates.append((mid, dict(updates)))
        cell = self.cells[mid]
        for k, v in updates.items():
            if k == "state":
                cell.state = MemoryState(v) if isinstance(v, str) else v
            else:
                setattr(cell, k, v)
        return True


# ════════════════════════════════════════════════════════════════════════════
# DecayManager.run_passive_decay
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestRunPassiveDecay:
    async def test_archives_old_cold(self):
        repo = _FakeMongoRepo()
        # 100 天前创建,COLD → 应被归档
        cell = _cell(state=MemoryState.COLD, days_ago=100)
        repo.cells[cell.mem_cell_id] = cell
        dm = DecayManager(repo, FSFMScorer())
        out = await dm.run_passive_decay("t1", "u1", now=_now())
        assert out["archived_count"] == 1
        assert repo.cells[cell.mem_cell_id].state == MemoryState.ARCHIVED

    async def test_keeps_recent_cold(self):
        repo = _FakeMongoRepo()
        # 5 天前 COLD → trs_score(half_life=30) ≈ 0.89 > 0.15 且 age<90 → 不归档
        cell = _cell(state=MemoryState.COLD, days_ago=5)
        repo.cells[cell.mem_cell_id] = cell
        dm = DecayManager(repo, FSFMScorer())
        out = await dm.run_passive_decay("t1", "u1", now=_now())
        assert out["archived_count"] == 0

    async def test_low_retention_archives(self):
        repo = _FakeMongoRepo()
        # 80 天前(retention < 0.15 if half_life=30 → exp(-ln2*80/30)≈0.16,
        # 接近阈值);用 35 天 + half_life=10 模拟低保留
        cell = _cell(state=MemoryState.COLD, days_ago=35)
        repo.cells[cell.mem_cell_id] = cell
        # 设置 trs_half_life=5 让 35 天保留度极低
        scorer = FSFMScorer()
        scorer.config.trs_half_life_days = 5.0
        dm = DecayManager(repo, scorer)
        out = await dm.run_passive_decay("t1", "u1", now=_now())
        assert out["archived_count"] == 1

    async def test_dry_run_no_persist(self):
        repo = _FakeMongoRepo()
        cell = _cell(state=MemoryState.COLD, days_ago=100)
        repo.cells[cell.mem_cell_id] = cell
        dm = DecayManager(repo, FSFMScorer())
        out = await dm.run_passive_decay("t1", "u1", now=_now(), dry_run=True)
        assert out["archived_count"] == 0
        # candidate_ids 仍标识出该记忆
        assert cell.mem_cell_id in out["candidate_ids"]
        # 实际 state 未变
        assert repo.cells[cell.mem_cell_id].state == MemoryState.COLD

    async def test_only_cold_scanned(self):
        repo = _FakeMongoRepo()
        active = _cell(state=MemoryState.ACTIVE, days_ago=200)
        repo.cells[active.mem_cell_id] = active
        dm = DecayManager(repo, FSFMScorer())
        out = await dm.run_passive_decay("t1", "u1", now=_now())
        # ACTIVE 不在扫描范围
        assert out["scanned_count"] == 0
        assert out["archived_count"] == 0


# ════════════════════════════════════════════════════════════════════════════
# DecayManager.enforce_capacity
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestEnforceCapacity:
    async def test_under_capacity_noop(self):
        repo = _FakeMongoRepo()
        for i in range(5):
            c = _cell(text=f"m{i}")
            repo.cells[c.mem_cell_id] = c
        dm = DecayManager(repo, FSFMScorer(), config=DecayConfig(max_memories_per_user=10))
        out = await dm.enforce_capacity("t1", "u1")
        assert out["archived_count"] == 0

    async def test_over_capacity_archives_low_score(self):
        repo = _FakeMongoRepo()
        # 创建 20 条:10 高分,10 低分
        for i in range(10):
            high = _cell(text="x" * 500, strength=3.0, access_count=5, days_ago=1)
            repo.cells[high.mem_cell_id] = high
        for i in range(10):
            low = _cell(text="x", strength=0.1, access_count=0, days_ago=200)
            repo.cells[low.mem_cell_id] = low
        # max=15,safety_margin=1.0(允许全部一次删完)
        dm = DecayManager(
            repo, FSFMScorer(),
            config=DecayConfig(max_memories_per_user=15, safety_margin=1.0),
        )
        out = await dm.enforce_capacity("t1", "u1")
        assert out["archived_count"] >= 5  # overflow=5

    async def test_safety_margin_caps(self):
        repo = _FakeMongoRepo()
        for _ in range(20):
            c = _cell()
            repo.cells[c.mem_cell_id] = c
        # max=10,overflow=10,但 safety_margin=0.1 → cap=2
        dm = DecayManager(
            repo, FSFMScorer(),
            config=DecayConfig(max_memories_per_user=10, safety_margin=0.1),
        )
        out = await dm.enforce_capacity("t1", "u1")
        assert out["archived_count"] == 2  # 安全边际限制

    async def test_dry_run(self):
        repo = _FakeMongoRepo()
        for _ in range(5):
            c = _cell()
            repo.cells[c.mem_cell_id] = c
        dm = DecayManager(
            repo, FSFMScorer(),
            config=DecayConfig(max_memories_per_user=2, safety_margin=1.0),
        )
        out = await dm.enforce_capacity("t1", "u1", dry_run=True)
        assert out["archived_count"] == 0
        assert len(out["candidate_ids"]) >= 1

    async def test_already_archived_excluded(self):
        repo = _FakeMongoRepo()
        for _ in range(3):
            c = _cell(state=MemoryState.ARCHIVED)
            repo.cells[c.mem_cell_id] = c
        for _ in range(5):
            c = _cell()
            repo.cells[c.mem_cell_id] = c
        dm = DecayManager(
            repo, FSFMScorer(),
            config=DecayConfig(max_memories_per_user=4, safety_margin=1.0),
        )
        out = await dm.enforce_capacity("t1", "u1")
        # total=8, overflow=4, 排除已 ARCHIVED 后 5 条候选
        assert out["archived_count"] == 4

    async def test_parse_decay_config(self):
        cfg = parse_decay_config({"max_memories_per_user": 50, "safety_margin": 0.2})
        assert cfg.max_memories_per_user == 50
        assert cfg.safety_margin == 0.2


# ════════════════════════════════════════════════════════════════════════════
# GreedyCapacityOptimizer 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestGreedyCapacityOptimizer:
    async def test_under_capacity_returns_empty(self):
        plugin = GreedyCapacityOptimizer()
        await plugin.start({})
        refs = [
            MemoryRef(memory_id=f"m{i}", memory_type="EPISODIC",
                      strength=1.0, access_count=0, created_at=_now())
            for i in range(3)
        ]
        out = await plugin.select_to_forget(refs, capacity=5)
        assert out == []

    async def test_state_priority(self):
        """ARCHIVED 应优先于 COLD 优先于 WARM 优先于 ACTIVE。"""
        plugin = GreedyCapacityOptimizer()
        await plugin.start({"safety_margin": 1.0})
        refs = [
            MemoryRef(memory_id="active", memory_type="EPISODIC",
                      state=MemoryState.ACTIVE,
                      strength=1.0, access_count=0, created_at=_now()),
            MemoryRef(memory_id="cold", memory_type="EPISODIC",
                      state=MemoryState.COLD,
                      strength=1.0, access_count=0, created_at=_now()),
            MemoryRef(memory_id="archived", memory_type="EPISODIC",
                      state=MemoryState.ARCHIVED,
                      strength=1.0, access_count=0, created_at=_now()),
            MemoryRef(memory_id="warm", memory_type="EPISODIC",
                      state=MemoryState.WARM,
                      strength=1.0, access_count=0, created_at=_now()),
        ]
        out = await plugin.select_to_forget(refs, capacity=2)
        # 4 → 2,需删 2 条:archived + cold
        assert "archived" in out
        assert "cold" in out
        assert "active" not in out

    async def test_strength_breaks_ties(self):
        plugin = GreedyCapacityOptimizer()
        await plugin.start({"safety_margin": 1.0})
        refs = [
            MemoryRef(memory_id="weak", memory_type="EPISODIC",
                      state=MemoryState.COLD,
                      strength=0.1, access_count=0, created_at=_now()),
            MemoryRef(memory_id="strong", memory_type="EPISODIC",
                      state=MemoryState.COLD,
                      strength=4.0, access_count=10, created_at=_now()),
        ]
        out = await plugin.select_to_forget(refs, capacity=1)
        assert out == ["weak"]

    async def test_safety_margin_caps(self):
        plugin = GreedyCapacityOptimizer()
        await plugin.start({"safety_margin": 0.1})
        refs = [
            MemoryRef(memory_id=f"m{i}", memory_type="EPISODIC",
                      state=MemoryState.COLD,
                      strength=0.1, access_count=0, created_at=_now())
            for i in range(20)
        ]
        # capacity=0 → overflow=20,但 cap=20*0.1=2
        out = await plugin.select_to_forget(refs, capacity=0)
        assert len(out) == 2

    async def test_danger_p0_priority(self):
        plugin = GreedyCapacityOptimizer()
        await plugin.start({"safety_margin": 1.0, "danger_threshold": -5.0})
        refs = [
            MemoryRef(memory_id="danger", memory_type="EPISODIC",
                      state=MemoryState.ACTIVE, strength=4.0, access_count=10,
                      importance_score=-9.0,
                      created_at=_now()),
            MemoryRef(memory_id="normal", memory_type="EPISODIC",
                      state=MemoryState.ACTIVE, strength=1.0, access_count=0,
                      importance_score=0.0,
                      created_at=_now()),
        ]
        out = await plugin.select_to_forget(refs, capacity=1)
        # danger 强制进入候选,即便 strength 高
        assert "danger" in out

    async def test_health(self):
        plugin = GreedyCapacityOptimizer()
        await plugin.start({"safety_margin": 0.2})
        h = await plugin.health()
        assert h["status"] == "ok"
        assert "0.2" in h["detail"]
