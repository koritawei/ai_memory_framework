"""Demo: Phase 3 写入冷路径(``ColdPathPipeline``)。

═══════════════════════════════════════════════════════════════════════════════
本 demo 走读
═══════════════════════════════════════════════════════════════════════════════
冷路径是热路径的"异步延伸":写入热路径只保证 SOT 落库,LLM 抽取 / 聚类 /
实体索引等"耗时但非必要"的工作走 ``BackgroundTaskRunner`` 异步执行,失败入 DLQ。

::

  MemCell (热路径落库后)
       │
       ▼  ColdPathPipeline.execute(cell)
       │
       ├── Stage 1: EpisodeExtractStage (LLM 情景抽取)
       │       → ctx.episodes: list[EpisodicMemory]
       │
       ├── Stage 2: SemanticExtractStage (LLM 语义联想,并行 gather)
       │       → ctx.semantics: list[SemanticMemory]
       │
       ├── Stage 3: ClusterStage (IncrementalCentroidClusterer)
       │       → ctx.cluster_id, ctx.cluster_meta
       │
       └── Stage 4: EntityIndexStage (Phase 7)
               → EntityStore.upsert_entities + MemoryGraph.add_memory_node

本 demo 用 fake 抽取器 / fake 聚类器,因为 demo 关注的是"管线编排"——
LLM 抽取本身的算法在 ``test_episode_extractor.py`` / ``test_semantic_extractor.py``
里测过了。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from memory_app.internal_models import (
    EpisodicMemory,
    KnowledgeType,
    MemCell,
    MemScene,
    SemanticMemory,
)
from memory_app.pipelines import ColdPathContext, ColdPathPipeline
from memory_app.pipelines.cold_path import (
    ClusterStage,
    EntityIndexStage,
    EpisodeExtractStage,
    SemanticExtractStage,
)
from memory_app.plugins.spi.clusterer import ClusterAssignmentMeta


# ════════════════════════════════════════════════════════════════════════════
# Demo 用 fake 抽取器 —— 记录"被调用了几次 / 用了多长时间"
# ════════════════════════════════════════════════════════════════════════════
class _FakeEpisodeExtractor:
    """模拟 LLM 抽取:从 cell.text 抽出 2 条 EpisodicMemory(带实体)。"""

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, cell: MemCell, old_memories=None, scenario=None):
        self.calls += 1
        # 真实 LLM 会调 OpenAI/Anthropic;这里返回固定 2 条作为 demo 输出
        return [
            EpisodicMemory(
                mem_cell_id=cell.mem_cell_id,
                tenant_id=cell.tenant_id,
                user_id=cell.user_id,
                summary=f"用户在 {cell.text[:10]} 提到了北京出差",
                key_entities=["北京", "出差"],
                emotional_valence=0.3,
            ),
            EpisodicMemory(
                mem_cell_id=cell.mem_cell_id,
                tenant_id=cell.tenant_id,
                user_id=cell.user_id,
                summary=f"用户在 {cell.text[:10]} 提到了咖啡馆",
                key_entities=["咖啡馆"],
                emotional_valence=0.5,
            ),
        ]


class _FakeSemanticExtractor:
    """模拟 LLM 语义联想:每条 episode 联想出 1 条 SemanticMemory。

    关键:记录每次 extract_for_episode 的进入时刻,demo 可断言"并行调用"
    (而不是 SemanticExtractStage 串行 await 等待)。
    """

    def __init__(self, sleep_s: float = 0.02) -> None:
        self.calls: list[float] = []
        self._sleep_s = sleep_s

    async def extract_for_episode(self, episode: EpisodicMemory):
        self.calls.append(time.monotonic())
        # 模拟 LLM 网络延迟,让"并行 vs 串行"在断言里可观察
        await asyncio.sleep(self._sleep_s)
        return [
            SemanticMemory(
                tenant_id=episode.tenant_id,
                user_id=episode.user_id,
                content=f"用户的偏好:{episode.summary[-10:]}",
                knowledge_type=KnowledgeType.PREFERENCE,
                source_episode_ids=[episode.episode_id],
                source_memcell_ids=[episode.mem_cell_id],
                confidence=0.9,
            )
        ]


class _FakeClusterer:
    """模拟聚类:固定返回 ``scene_xxx``,标记为 new_cluster。"""

    def __init__(self) -> None:
        self.assignments: list[tuple[str, str]] = []

    async def cluster(self, group_id: str, cell: MemCell):
        scene_id = f"scene-{group_id}-{cell.mem_cell_id[:6]}"
        self.assignments.append((group_id, scene_id))
        return scene_id, ClusterAssignmentMeta(
            similarity=0.0, is_new_cluster=True
        )


# ════════════════════════════════════════════════════════════════════════════
# 1. 一条 MemCell 完整跑完四个阶段
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_cold_path_runs_all_four_stages(
    fake_entity_store, fake_memory_graph
):
    """一条 MemCell → 2 episodes → 2 semantics → cluster + 3 entities 上图。

    断言每个 stage 都对 ctx / 外部副作用产生了预期影响。
    """
    # ── 输入 ──────────────────────────────────────────────────────────────
    cell = MemCell(
        tenant_id="t1", user_id="u1", session_id="s1",
        text="今天聊到北京出差和咖啡馆话题",
        timestamp=datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
    )

    # ── 装配 ──────────────────────────────────────────────────────────────
    ep_ext = _FakeEpisodeExtractor()
    sem_ext = _FakeSemanticExtractor(sleep_s=0.02)
    clusterer = _FakeClusterer()

    pipeline = ColdPathPipeline(
        episode_extractor=ep_ext,
        semantic_extractor=sem_ext,
        clusterer=clusterer,
    )
    # EntityIndexStage 不在 ColdPathPipeline 的默认 stages 里(Phase 7 才接入)。
    # 装配代码通过 ``extra_stages`` 把它推入。这里 demo 直接 push 到 pipeline。
    entity_stage = EntityIndexStage(
        entity_store=fake_entity_store,
        memory_graph=fake_memory_graph,
    )
    pipeline._extra_stages.append(entity_stage)

    # ── 执行 ──────────────────────────────────────────────────────────────
    ctx = await pipeline.execute(cell)

    # ── Stage 1 断言:Episode 抽取 ──────────────────────────────────────
    assert ep_ext.calls == 1, "EpisodeExtractStage 调一次 LLM(整条 cell)"
    assert len(ctx.episodes) == 2
    summaries = "|".join(ep.summary for ep in ctx.episodes)
    assert "北京出差" in summaries
    assert "咖啡馆" in summaries

    # ── Stage 2 断言:Semantic 抽取,**每个 episode 一次 LLM** ────────
    assert len(sem_ext.calls) == 2
    assert len(ctx.semantics) == 2
    assert all(s.knowledge_type == KnowledgeType.PREFERENCE for s in ctx.semantics)

    # ── Stage 3 断言:Cluster ───────────────────────────────────────────
    assert ctx.cluster_id is not None
    assert ctx.cluster_id.startswith("scene-")
    assert ctx.cluster_meta.is_new_cluster is True
    assert ctx.metrics["cluster_is_new"] is True

    # ── Stage 4 断言:EntityIndex(EntityStore + MemoryGraph 都被调到)──
    # 2 个 episode 各自 key_entities,去重后:"北京", "出差", "咖啡馆"
    assert len(fake_entity_store.upsert_calls) == 1
    mid, ents = fake_entity_store.upsert_calls[0]
    assert mid == cell.mem_cell_id
    assert set(ents) == {"北京", "出差", "咖啡馆"}
    assert ctx.metrics["entity_index_count"] == 3

    # MemoryGraph 也收到了 add_memory_node
    assert len(fake_memory_graph.add_memory_calls) == 1
    g_mid, g_ents, g_tenant, g_user = fake_memory_graph.add_memory_calls[0]
    assert g_mid == cell.mem_cell_id
    assert set(g_ents) == {"北京", "出差", "咖啡馆"}
    assert g_tenant == "t1" and g_user == "u1"
    assert ctx.metrics["graph_entity_count"] == 3


# ════════════════════════════════════════════════════════════════════════════
# 2. SemanticExtractStage 的并发性:gather 让两次 LLM 并行而非串行
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_semantic_stage_parallelizes_llm_calls():
    """两个 episode + 每次 LLM sleep 50ms → 并行总耗时应 ≈ 50ms(单次),
    而非串行的 100ms。

    上一轮修复(把 for 循环改为 ``asyncio.gather``)的回归守护点。
    """
    ep_ext = _FakeEpisodeExtractor()
    sem_ext = _FakeSemanticExtractor(sleep_s=0.05)
    clusterer = _FakeClusterer()

    pipeline = ColdPathPipeline(
        episode_extractor=ep_ext,
        semantic_extractor=sem_ext,
        clusterer=clusterer,
    )
    cell = MemCell(
        tenant_id="t1", user_id="u1", session_id="s1",
        text="并发测试",
    )

    start = time.monotonic()
    ctx = await pipeline.execute(cell)
    elapsed = time.monotonic() - start

    assert len(ctx.semantics) == 2
    # 串行总耗时 = 2×50ms = 100ms;并行 ≈ 50ms。
    # 给宽松上限 90ms,既排除串行又给 CI 抖动留余量。
    assert elapsed < 0.09, (
        f"SemanticExtractStage 应并发 LLM 调用,实测 {elapsed*1000:.1f}ms"
    )


# ════════════════════════════════════════════════════════════════════════════
# 3. EpisodeExtractStage 异常:整段冷路径不抛(由 BackgroundTaskRunner 重试)
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_cold_path_propagates_extractor_exception():
    """LLM 抽取抛错时,冷路径会**冒泡**到调用方 ——
    由 ``BackgroundTaskRunner._run_with_retry`` 触发重试 / DLQ。

    这不同于 Stage 4 EntityIndex 的"软失败" —— Stage 1/2 的异常代表 LLM 不可用,
    重试有意义;Stage 4 的异常代表"实体索引坏了",不应阻塞情景/语义已落库的事实。
    """
    class _BoomEpisode:
        async def extract(self, cell, **kw):
            raise RuntimeError("LLM 503")

    pipeline = ColdPathPipeline(
        episode_extractor=_BoomEpisode(),
        semantic_extractor=_FakeSemanticExtractor(),
        clusterer=_FakeClusterer(),
    )
    cell = MemCell(tenant_id="t1", user_id="u1", session_id="s1", text="boom")

    with pytest.raises(RuntimeError, match="LLM 503"):
        await pipeline.execute(cell)


# ════════════════════════════════════════════════════════════════════════════
# 4. EntityIndexStage 软失败:store 报错时 graph 仍写入,ctx 记 warning
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_entity_index_soft_fails_when_store_raises(fake_memory_graph):
    """演示设计文档 §5.4 的"软失败"语义:Phase 7 实体索引坏了不该让
    Phase 3 已抽取的 episodes/semantics 白白丢掉。"""
    class _BoomStore:
        async def upsert_entities(self, *a, **kw):
            raise RuntimeError("EntityStore down")

    pipeline = ColdPathPipeline(
        episode_extractor=_FakeEpisodeExtractor(),
        semantic_extractor=_FakeSemanticExtractor(),
        clusterer=_FakeClusterer(),
    )
    pipeline._extra_stages.append(
        EntityIndexStage(
            entity_store=_BoomStore(),
            memory_graph=fake_memory_graph,
        )
    )

    cell = MemCell(tenant_id="t1", user_id="u1", session_id="s1", text="软失败演练")
    ctx = await pipeline.execute(cell)

    # 关键不变量:ctx 仍包含完整的 episodes / semantics
    assert len(ctx.episodes) == 2
    assert len(ctx.semantics) == 2
    # warning 留痕,运维可观察
    assert any("entity_store_upsert_failed" in w for w in ctx.warnings)
    # MemoryGraph 不受 EntityStore 故障影响,正常写入
    assert len(fake_memory_graph.add_memory_calls) == 1
