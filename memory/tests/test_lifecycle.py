"""LifecycleUpdater 测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.background import BackgroundTaskRunner
from memory_app.internal_models import MemCell, MemoryState
from memory_app.lifecycle import LifecycleUpdater, compute_state


class _FakeMongoRepo:
    def __init__(self):
        self.store: dict[str, MemCell] = {}
        self.updates: list[tuple[str, dict]] = []

    async def insert(self, cell):
        self.store[cell.mem_cell_id] = cell
        return cell.mem_cell_id

    async def get_by_id(self, mid):
        return self.store.get(mid)

    async def update(self, mid, updates):
        if mid not in self.store:
            return False
        self.updates.append((mid, dict(updates)))
        cell = self.store[mid]
        for k, v in updates.items():
            if k == "state":
                setattr(cell, "state", MemoryState(v) if isinstance(v, str) else v)
            else:
                setattr(cell, k, v)
        return True


def _cell(strength=1.0, access_count=0, days_ago=0.0) -> MemCell:
    return MemCell(
        tenant_id="t1", user_id="u1", session_id="s1",
        text="x",
        strength=strength,
        access_count=access_count,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


# ════════════════════════════════════════════════════════════════════════════
# compute_state
# ════════════════════════════════════════════════════════════════════════════
class TestComputeState:
    def test_active_high_access(self):
        s = compute_state(
            access_count=10,
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        assert s == MemoryState.ACTIVE

    def test_active_recent(self):
        s = compute_state(
            access_count=0,
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        assert s == MemoryState.ACTIVE

    def test_warm_medium(self):
        s = compute_state(
            access_count=2,
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        assert s == MemoryState.WARM

    def test_warm_within_week(self):
        s = compute_state(
            access_count=0,
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        assert s == MemoryState.WARM

    def test_cold_within_quarter(self):
        s = compute_state(
            access_count=0,
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        assert s == MemoryState.COLD

    def test_archived_old(self):
        s = compute_state(
            access_count=0,
            created_at=datetime.now(timezone.utc) - timedelta(days=120),
        )
        assert s == MemoryState.ARCHIVED


# ════════════════════════════════════════════════════════════════════════════
# LifecycleUpdater 同步路径
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestLifecycleUpdaterSync:
    async def test_update_now_increments(self):
        repo = _FakeMongoRepo()
        cell = _cell(strength=1.0, access_count=0)
        await repo.insert(cell)
        updater = LifecycleUpdater(mongo_repo=repo)
        out = await updater.update_now(cell.mem_cell_id)
        assert out is not None
        assert out["strength"] == pytest.approx(1.1)
        assert out["access_count"] == 1
        assert out["state"] == MemoryState.ACTIVE
        # 持久化生效
        assert repo.store[cell.mem_cell_id].strength == pytest.approx(1.1)

    async def test_update_now_clamps_smax(self):
        repo = _FakeMongoRepo()
        cell = _cell(strength=9.95, access_count=0)
        await repo.insert(cell)
        updater = LifecycleUpdater(mongo_repo=repo, s_max=10.0)
        out = await updater.update_now(cell.mem_cell_id)
        assert out["strength"] == 10.0  # clamp

    async def test_update_now_missing(self):
        repo = _FakeMongoRepo()
        updater = LifecycleUpdater(mongo_repo=repo)
        out = await updater.update_now("not-here")
        assert out is None

    async def test_state_progresses_to_active_on_high_access(self):
        repo = _FakeMongoRepo()
        # 起始 access_count=4,age=10d → COLD
        cell = _cell(access_count=4, days_ago=10)
        await repo.insert(cell)
        updater = LifecycleUpdater(mongo_repo=repo)
        out = await updater.update_now(cell.mem_cell_id)
        # access +1 → 5,触发 ACTIVE
        assert out["access_count"] == 5
        assert out["state"] == MemoryState.ACTIVE


# ════════════════════════════════════════════════════════════════════════════
# 异步入口(BackgroundTaskRunner)
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestLifecycleUpdaterAsync:
    async def test_on_retrieval_hit_via_runner(self):
        repo = _FakeMongoRepo()
        cell = _cell()
        await repo.insert(cell)
        runner = BackgroundTaskRunner()
        updater = LifecycleUpdater(mongo_repo=repo, runner=runner)
        updater.on_retrieval_hit([cell.mem_cell_id])
        await runner.shutdown()
        # 持久化生效
        assert repo.store[cell.mem_cell_id].access_count == 1

    async def test_multiple_ids_each_independent(self):
        repo = _FakeMongoRepo()
        cells = [_cell() for _ in range(3)]
        for c in cells:
            await repo.insert(c)
        runner = BackgroundTaskRunner()
        updater = LifecycleUpdater(mongo_repo=repo, runner=runner)
        updater.on_retrieval_hit([c.mem_cell_id for c in cells])
        await runner.shutdown()
        for c in cells:
            assert repo.store[c.mem_cell_id].access_count == 1

    async def test_empty_list_noop(self):
        repo = _FakeMongoRepo()
        runner = BackgroundTaskRunner()
        updater = LifecycleUpdater(mongo_repo=repo, runner=runner)
        updater.on_retrieval_hit([])
        await runner.shutdown()
        assert repo.updates == []

    async def test_stats(self):
        repo = _FakeMongoRepo()
        cell = _cell()
        await repo.insert(cell)
        runner = BackgroundTaskRunner()
        updater = LifecycleUpdater(mongo_repo=repo, runner=runner)
        updater.on_retrieval_hit([cell.mem_cell_id])
        await runner.shutdown()
        s = updater.stats()
        assert s["submitted"] == 1
        assert s["completed"] == 1
