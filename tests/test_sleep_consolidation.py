"""SleepConsolidator + ThreePhaseDreamingStrategy 测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory_app.consolidation.sleep import SleepConsolidator
from memory_app.consolidator import Consolidator as CoreConsolidator
from memory_app.internal_models import (
    KnowledgeType,
    MemCell,
    MemScene,
    MemoryState,
    SemanticMemory,
)
from memory_app.plugins.spi.consolidator import ConsolidationDecision
from memory_app.plugins_default.three_phase_dreaming import (
    ThreePhaseDreamingStrategy,
)


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeRepo:
    def __init__(self):
        self.cells: dict[str, MemCell] = {}

    async def insert(self, cell):
        self.cells[cell.mem_cell_id] = cell

    async def get_by_id(self, mid):
        return self.cells.get(mid)


class _FakeLLM:
    def __init__(self, responses=None, fail=False):
        self.responses = list(responses or [])
        self.calls: list[str] = []
        self.fail = fail

    async def generate(self, prompt, **_):
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("llm down")
        if not self.responses:
            return "[]"
        return self.responses.pop(0)


def _scene(member_ids: list[str]) -> MemScene:
    return MemScene(
        tenant_id="t1",
        user_id="u1",
        member_episode_ids=list(member_ids),
        member_count=len(member_ids),
    )


# ════════════════════════════════════════════════════════════════════════════
# SleepConsolidator
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestSleepConsolidator:
    async def test_immature_scene_returns_empty(self):
        repo = _FakeRepo()
        await repo.insert(MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="x", mem_cell_id="m1",
        ))
        sc = SleepConsolidator(_FakeLLM(), repo, CoreConsolidator())
        out = await sc.consolidate_scene(_scene(["m1"]))
        assert out == []

    async def test_mature_scene_produces_semantics(self):
        repo = _FakeRepo()
        for i in range(3):
            await repo.insert(MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text=f"用户喜欢咖啡 #{i}", mem_cell_id=f"m{i}",
            ))
        llm = _FakeLLM([
            '[{"content":"用户是咖啡爱好者","knowledge_type":"preference","confidence":0.9}]'
        ])
        sc = SleepConsolidator(llm, repo, CoreConsolidator())
        out = await sc.consolidate_scene(_scene(["m0", "m1", "m2"]))
        assert len(out) == 1
        assert "咖啡" in out[0].content
        assert out[0].knowledge_type == KnowledgeType.PREFERENCE

    async def test_llm_failure_safe_empty(self):
        repo = _FakeRepo()
        for i in range(3):
            await repo.insert(MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text=f"x{i}", mem_cell_id=f"m{i}",
            ))
        sc = SleepConsolidator(_FakeLLM(fail=True), repo, CoreConsolidator())
        out = await sc.consolidate_scene(_scene(["m0", "m1", "m2"]))
        assert out == []

    async def test_invalid_json_safe_empty(self):
        repo = _FakeRepo()
        for i in range(3):
            await repo.insert(MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text=f"x{i}", mem_cell_id=f"m{i}",
            ))
        sc = SleepConsolidator(_FakeLLM(["not json"]), repo, CoreConsolidator())
        out = await sc.consolidate_scene(_scene(["m0", "m1", "m2"]))
        assert out == []

    async def test_unbound_llm_returns_empty(self):
        repo = _FakeRepo()
        for i in range(3):
            await repo.insert(MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text=f"x{i}", mem_cell_id=f"m{i}",
            ))
        sc = SleepConsolidator(None, repo, CoreConsolidator())
        out = await sc.consolidate_scene(_scene(["m0", "m1", "m2"]))
        assert out == []

    async def test_consolidator_noop_filters(self):
        """Consolidator NOOP 决策时,候选不进入返回列表。"""
        repo = _FakeRepo()
        for i in range(3):
            await repo.insert(MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text=f"x{i}", mem_cell_id=f"m{i}",
            ))
        # 已有事实与 LLM 输出相同
        existing = [
            SemanticMemory(
                tenant_id="t1", user_id="u1",
                content="用户是咖啡爱好者", knowledge_type=KnowledgeType.PREFERENCE,
            )
        ]
        llm = _FakeLLM([
            '[{"content":"用户是咖啡爱好者","knowledge_type":"preference"}]'
        ])
        sc = SleepConsolidator(llm, repo, CoreConsolidator())
        out = await sc.consolidate_scene(_scene(["m0", "m1", "m2"]), existing_facts=existing)
        assert out == []  # NOOP 过滤

    async def test_consolidate_scenes_batch(self):
        repo = _FakeRepo()
        for i in range(6):
            await repo.insert(MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text=f"x{i}", mem_cell_id=f"m{i}",
            ))
        llm = _FakeLLM([
            '[{"content":"AAAAA"}]',
            '[{"content":"BBBBB"}]',
        ])
        sc = SleepConsolidator(llm, repo, CoreConsolidator())
        scenes = [_scene(["m0", "m1", "m2"]), _scene(["m3", "m4", "m5"])]
        out = await sc.consolidate_scenes(scenes)
        assert len(out) == 2


# ════════════════════════════════════════════════════════════════════════════
# ThreePhaseDreamingStrategy
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestThreePhaseDreaming:
    async def test_disabled_phase_returns_noop_report(self):
        s = ThreePhaseDreamingStrategy()
        await s.start({"light": {"enabled": False}})
        report = await s.run(scope="light")
        assert report.phase == "light"
        assert any("disabled" in n for n in report.notes)
        assert report.scanned_count == 0

    async def test_explicit_scope(self):
        s = ThreePhaseDreamingStrategy()
        await s.start({})
        report = await s.run(scope="rem", time=datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert report.phase == "rem"

    async def test_auto_scope_picks_deep_at_03h(self):
        s = ThreePhaseDreamingStrategy()
        await s.start({})
        # 周一 03:30 UTC
        report = await s.run(
            scope="all", time=datetime(2026, 6, 1, 3, 30, tzinfo=timezone.utc)
        )
        assert report.phase == "deep"

    async def test_auto_scope_picks_rem_on_sunday_05h(self):
        s = ThreePhaseDreamingStrategy()
        await s.start({})
        # 周日(weekday=6) 05:30 UTC
        # 2026-05-31 是周日
        report = await s.run(
            scope="all", time=datetime(2026, 5, 31, 5, 30, tzinfo=timezone.utc)
        )
        assert report.phase == "rem"

    async def test_run_with_components(self):
        repo = _FakeRepo()
        for i in range(3):
            await repo.insert(MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text=f"咖啡 {i}", mem_cell_id=f"m{i}",
            ))
        llm = _FakeLLM(['[{"content":"用户喜欢咖啡","confidence":0.9}]'])
        sleep = SleepConsolidator(llm, repo, CoreConsolidator())

        s = ThreePhaseDreamingStrategy()
        await s.start({})

        async def _scope_provider():
            return [("t1", "u1")]

        async def _scenes_provider(tid, uid):
            return [_scene(["m0", "m1", "m2"])]

        s.bind_pipeline_components(
            sleep=sleep,
            decay=None,
            scenes_provider=_scenes_provider,
            scope_provider=_scope_provider,
        )
        report = await s.run(scope="light")
        assert report.scanned_count == 1
        assert report.consolidated_count == 1
        assert report.error_count == 0

    async def test_health(self):
        s = ThreePhaseDreamingStrategy()
        await s.start({})
        h = await s.health()
        assert h["status"] == "ok"
        assert "light" in h["detail"]
