"""ColdPathPipeline + ColdPathService + BackgroundTaskRunner 测试(Phase 3 配套)。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from memory_app.background import BackgroundTaskRunner, RetryPolicy
from memory_app.internal_models import (
    EpisodicMemory,
    KnowledgeType,
    MemCell,
    SemanticMemory,
)
from memory_app.pipelines import (
    ColdPathContext,
    ColdPathPipeline,
)
from memory_app.repositories.dlq import InMemoryDLQ
from memory_app.services import ColdPathService


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
def _cell(text: str = "我下周要去北京", emb: list[float] | None = None) -> MemCell:
    return MemCell(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        text=text,
        embedding=emb,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class _FakeEpisodeExtractor:
    def __init__(self, episodes: list[EpisodicMemory] | None = None, fail: bool = False):
        self._episodes = episodes
        self.fail = fail
        self.calls = 0

    async def extract(self, memcell, old_memories=None, scenario=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("extract failed")
        if self._episodes is not None:
            return list(self._episodes)
        return [
            EpisodicMemory(
                episode_id=f"ep-{memcell.mem_cell_id}",
                mem_cell_id=memcell.mem_cell_id,
                tenant_id=memcell.tenant_id,
                user_id=memcell.user_id,
                summary=f"summary of {memcell.text[:20]}",
            )
        ]


class _FakeSemanticExtractor:
    def __init__(self, items: list[SemanticMemory] | None = None, fail: bool = False):
        self._items = items
        self.fail = fail
        self.calls = 0

    async def extract_for_episode(self, episode):
        self.calls += 1
        if self.fail:
            raise RuntimeError("semantic failed")
        if self._items is not None:
            return list(self._items)
        return [
            SemanticMemory(
                tenant_id=episode.tenant_id,
                user_id=episode.user_id,
                content=f"semantic from {episode.episode_id}",
                knowledge_type=KnowledgeType.FACT,
                source_episode_ids=[episode.episode_id],
            )
        ]


class _FakeClusterer:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def cluster(self, group_id: str, memcell):
        self.calls.append((group_id, memcell.mem_cell_id))
        if self.fail:
            raise RuntimeError("cluster failed")
        from memory_app.plugins.spi.clusterer import ClusterAssignmentMeta

        return f"sc-{memcell.mem_cell_id}", ClusterAssignmentMeta(
            similarity=0.9, is_new_cluster=False
        )


# ════════════════════════════════════════════════════════════════════════════
# ColdPathPipeline
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestColdPathPipeline:
    async def test_full_pipeline_happy_path(self):
        pipe = ColdPathPipeline(
            episode_extractor=_FakeEpisodeExtractor(),
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(),
        )
        ctx = await pipe.execute(_cell())
        assert isinstance(ctx, ColdPathContext)
        assert len(ctx.episodes) == 1
        assert len(ctx.semantics) == 1
        assert ctx.cluster_id is not None
        assert ctx.metrics["episode_count"] == 1
        assert ctx.metrics["semantic_count"] == 1
        assert "cluster_id" in ctx.metrics

    async def test_episode_failure_propagates(self):
        pipe = ColdPathPipeline(
            episode_extractor=_FakeEpisodeExtractor(fail=True),
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(),
        )
        with pytest.raises(RuntimeError, match="extract failed"):
            await pipe.execute(_cell())

    async def test_semantic_failure_per_episode_isolated(self):
        """单条情景 semantic 失败不影响其他情景与下游聚类。"""
        eps = [
            EpisodicMemory(episode_id="e1", mem_cell_id="m1", tenant_id="t", user_id="u", summary="s1"),
            EpisodicMemory(episode_id="e2", mem_cell_id="m1", tenant_id="t", user_id="u", summary="s2"),
        ]
        sem = _FakeSemanticExtractor(fail=True)
        pipe = ColdPathPipeline(
            episode_extractor=_FakeEpisodeExtractor(eps),
            semantic_extractor=sem,
            clusterer=_FakeClusterer(),
        )
        ctx = await pipe.execute(_cell())
        # 全部 episode 都失败 → semantics 空,但流程继续
        assert ctx.semantics == []
        assert any("semantic_extract_failed" in w for w in ctx.warnings)
        assert ctx.cluster_id is not None  # 聚类仍执行

    async def test_cluster_failure_does_not_break(self):
        pipe = ColdPathPipeline(
            episode_extractor=_FakeEpisodeExtractor(),
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(fail=True),
        )
        ctx = await pipe.execute(_cell())
        assert ctx.episodes  # 情景已抽
        assert ctx.cluster_id is None
        assert any("cluster_failed" in w for w in ctx.warnings)

    async def test_unbound_components_emit_warnings(self):
        pipe = ColdPathPipeline()
        ctx = await pipe.execute(_cell())
        assert "episode_extractor_unbound" in ctx.warnings
        assert "semantic_extractor_unbound" in ctx.warnings
        assert "clusterer_unbound" in ctx.warnings

    async def test_no_episodes_skips_semantic(self):
        ep = _FakeEpisodeExtractor(episodes=[])  # 不产 episode
        pipe = ColdPathPipeline(
            episode_extractor=ep,
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(),
        )
        ctx = await pipe.execute(_cell())
        assert ctx.episodes == []
        assert ctx.semantics == []
        assert "no_episodes_for_semantic" in ctx.warnings


# ════════════════════════════════════════════════════════════════════════════
# BackgroundTaskRunner
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestBackgroundTaskRunner:
    async def test_submit_runs_coroutine(self):
        runner = BackgroundTaskRunner()
        ran = []

        async def task():
            ran.append("ok")

        t = runner.submit(task, task_id="t-1")
        await t
        assert ran == ["ok"]
        stats = runner.stats()
        assert stats["completed"] == 1
        assert stats["failed_to_dlq"] == 0

    async def test_retry_then_success(self):
        runner = BackgroundTaskRunner(
            retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.001)
        )
        attempts = {"n": 0}

        async def task():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")

        t = runner.submit(task, task_id="t-2")
        await t
        assert attempts["n"] == 3

    async def test_exhausted_goes_to_dlq(self):
        dlq = InMemoryDLQ()
        runner = BackgroundTaskRunner(
            dlq=dlq, retry_policy=RetryPolicy(max_attempts=2, base_delay_s=0.001)
        )

        async def task():
            raise RuntimeError("permanent")

        t = runner.submit(task, task_id="m1")
        await t  # 不应抛
        records = await dlq.list()
        assert len(records) == 1
        assert records[0].mem_cell_id == "m1"
        assert records[0].target == "background_task"
        stats = runner.stats()
        assert stats["failed_to_dlq"] == 1

    async def test_shutdown_waits_for_inflight(self):
        runner = BackgroundTaskRunner()
        marker = []

        async def task():
            await asyncio.sleep(0.01)
            marker.append("done")

        runner.submit(task, task_id="x")
        await runner.shutdown()
        assert marker == ["done"]

    async def test_submit_after_shutdown_raises(self):
        runner = BackgroundTaskRunner()
        await runner.shutdown()
        with pytest.raises(RuntimeError):
            runner.submit(lambda: _noop_coro(), task_id="x")


async def _noop_coro():
    return None


# ════════════════════════════════════════════════════════════════════════════
# ColdPathService
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestColdPathService:
    async def test_run_now_executes_pipeline(self):
        pipe = ColdPathPipeline(
            episode_extractor=_FakeEpisodeExtractor(),
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(),
        )
        svc = ColdPathService(pipeline=pipe)
        ctx = await svc.run_now(_cell())
        assert ctx.episodes and ctx.semantics

    async def test_schedule_via_runner(self):
        ep = _FakeEpisodeExtractor()
        pipe = ColdPathPipeline(
            episode_extractor=ep,
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(),
        )
        runner = BackgroundTaskRunner()
        svc = ColdPathService(pipeline=pipe, runner=runner)
        svc.schedule(_cell())
        await runner.shutdown()
        assert ep.calls == 1

    async def test_schedule_many(self):
        ep = _FakeEpisodeExtractor()
        pipe = ColdPathPipeline(
            episode_extractor=ep,
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(),
        )
        runner = BackgroundTaskRunner()
        svc = ColdPathService(pipeline=pipe, runner=runner)
        svc.schedule_many([_cell(), _cell(text="another")])
        await runner.shutdown()
        assert ep.calls == 2

    async def test_on_complete_callback(self):
        called: list = []

        async def cb(ctx):
            called.append(ctx)

        pipe = ColdPathPipeline(
            episode_extractor=_FakeEpisodeExtractor(),
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(),
        )
        svc = ColdPathService(pipeline=pipe, on_complete=cb)
        await svc.run_now(_cell())
        assert len(called) == 1

    async def test_pipeline_failure_goes_to_dlq(self):
        dlq = InMemoryDLQ()
        runner = BackgroundTaskRunner(
            dlq=dlq, retry_policy=RetryPolicy(max_attempts=1, base_delay_s=0.001)
        )
        pipe = ColdPathPipeline(
            episode_extractor=_FakeEpisodeExtractor(fail=True),
            semantic_extractor=_FakeSemanticExtractor(),
            clusterer=_FakeClusterer(),
        )
        svc = ColdPathService(pipeline=pipe, runner=runner)
        svc.schedule(_cell())
        await runner.shutdown()
        records = await dlq.list()
        assert len(records) == 1
        assert records[0].target == "cold_path"
