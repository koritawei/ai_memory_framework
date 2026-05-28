"""ClusterManager + IncrementalCentroidClusterer 测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.clustering import (
    ClusterManager,
    ClusterManagerConfig,
    cosine_similarity,
    parse_cluster_config,
    update_centroid,
)
from memory_app.internal_models import MemCell, MemScene
from memory_app.plugins_default.incremental_centroid import (
    IncrementalCentroidClusterer,
)


# ════════════════════════════════════════════════════════════════════════════
# 数学工具
# ════════════════════════════════════════════════════════════════════════════
class TestCosineSimilarity:
    def test_identical(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_opposite(self):
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty(self):
        assert cosine_similarity([], [1.0]) == 0.0
        assert cosine_similarity([1.0], []) == 0.0

    def test_dim_mismatch(self):
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_zero_vector(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


class TestUpdateCentroid:
    def test_first_member(self):
        result = update_centroid([], [1.0, 2.0], new_size=1)
        assert result == [1.0, 2.0]

    def test_increment(self):
        # 第二个 member 加入:centroid_old=[1, 2], new_emb=[3, 4]
        # → centroid_new = ([1, 2] * 1 + [3, 4]) / 2 = [2, 3]
        result = update_centroid([1.0, 2.0], [3.0, 4.0], new_size=2)
        assert result == [2.0, 3.0]

    def test_dim_mismatch_returns_new(self):
        result = update_centroid([1.0, 2.0], [1.0, 2.0, 3.0], new_size=2)
        assert result == [1.0, 2.0, 3.0]


class TestParseClusterConfig:
    def test_default(self):
        cfg = parse_cluster_config(None)
        assert cfg.similarity_threshold == 0.65
        assert cfg.time_gap_max == timedelta(days=7)
        assert cfg.max_scene_size == 50

    def test_custom(self):
        cfg = parse_cluster_config(
            {"similarity_threshold": 0.8, "time_gap_days": 3, "max_scene_size": 100}
        )
        assert cfg.similarity_threshold == 0.8
        assert cfg.time_gap_max == timedelta(days=3)
        assert cfg.max_scene_size == 100


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════
def _cell(emb: list[float] | None, *, days_ago: float = 0.0, **overrides) -> MemCell:
    base = dict(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        text="t",
        embedding=emb,
        timestamp=datetime(2026, 1, 10, tzinfo=timezone.utc) - timedelta(days=days_ago),
    )
    base.update(overrides)
    return MemCell(**base)


def _scene(centroid: list[float], members: list[str], *, days_ago: float = 0.0) -> MemScene:
    ts = datetime(2026, 1, 10, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return MemScene(
        tenant_id="t1",
        user_id="u1",
        centroid=centroid,
        member_episode_ids=list(members),
        member_count=len(members),
        last_updated_at=ts,
        created_at=ts,
    )


# ════════════════════════════════════════════════════════════════════════════
# ClusterManager
# ════════════════════════════════════════════════════════════════════════════
class TestClusterManager:
    def test_assign_to_existing_high_sim(self):
        mgr = ClusterManager()
        scene = _scene([1.0, 0.0, 0.0], ["old"])
        cell = _cell([0.99, 0.1, 0.0])
        decision = mgr.assign(cell, [scene])
        assert decision.is_new_cluster is False
        assert cell.mem_cell_id in decision.scene.member_episode_ids
        assert decision.scene.member_count == 2

    def test_create_new_low_sim(self):
        mgr = ClusterManager()
        scene = _scene([1.0, 0.0, 0.0], ["old"])
        cell = _cell([0.0, 1.0, 0.0])  # 正交
        decision = mgr.assign(cell, [scene])
        assert decision.is_new_cluster is True
        assert decision.scene is not scene

    def test_create_new_when_no_embedding(self):
        mgr = ClusterManager()
        scene = _scene([1.0, 0.0], ["old"])
        cell = _cell(None)
        decision = mgr.assign(cell, [scene])
        assert decision.is_new_cluster is True
        assert decision.scene.centroid is None

    def test_idempotent_when_already_member(self):
        mgr = ClusterManager()
        cell = _cell([1.0, 0.0])
        # 把 cell 已塞进 scene
        scene = _scene([1.0, 0.0], [cell.mem_cell_id])
        decision = mgr.assign(cell, [scene])
        # 不应重复加入,member_count 仍然是 1
        assert decision.scene.member_count == 1

    def test_scene_full_creates_new(self):
        cfg = ClusterManagerConfig()
        cfg.max_scene_size = 2
        mgr = ClusterManager(cfg)
        scene = _scene([1.0, 0.0], ["a", "b"])  # 已满
        cell = _cell([1.0, 0.0])
        decision = mgr.assign(cell, [scene])
        assert decision.is_new_cluster is True

    def test_time_gap_creates_new(self):
        mgr = ClusterManager()
        scene = _scene([1.0, 0.0], ["old"], days_ago=30)  # 30 天前
        cell = _cell([1.0, 0.0], days_ago=0)
        decision = mgr.assign(cell, [scene])
        assert decision.is_new_cluster is True  # 超时间窗

    def test_picks_best_similarity_among_candidates(self):
        mgr = ClusterManager()
        s1 = _scene([1.0, 0.0, 0.0], ["s1m"])
        s2 = _scene([0.9, 0.1, 0.0], ["s2m"])
        cell = _cell([0.95, 0.0, 0.0])  # 与 s1 更近
        decision = mgr.assign(cell, [s1, s2])
        assert decision.scene is s1


# ════════════════════════════════════════════════════════════════════════════
# IncrementalCentroidClusterer 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestIncrementalCentroidPlugin:
    async def test_first_cluster_is_new(self):
        plugin = IncrementalCentroidClusterer()
        await plugin.start({})
        cell = _cell([1.0, 0.0, 0.0])
        cid, meta = await plugin.cluster("g1", cell)
        assert meta.is_new_cluster is True
        assert cid

    async def test_second_similar_joins_existing(self):
        plugin = IncrementalCentroidClusterer()
        await plugin.start({})
        c1 = _cell([1.0, 0.0, 0.0])
        c2 = _cell([0.99, 0.1, 0.0])
        cid1, _ = await plugin.cluster("g1", c1)
        cid2, meta2 = await plugin.cluster("g1", c2)
        assert meta2.is_new_cluster is False
        assert cid1 == cid2

    async def test_dissimilar_creates_new(self):
        plugin = IncrementalCentroidClusterer()
        await plugin.start({})
        c1 = _cell([1.0, 0.0, 0.0])
        c2 = _cell([0.0, 1.0, 0.0])  # 正交
        cid1, _ = await plugin.cluster("g1", c1)
        cid2, meta2 = await plugin.cluster("g1", c2)
        assert meta2.is_new_cluster is True
        assert cid1 != cid2

    async def test_group_isolation(self):
        plugin = IncrementalCentroidClusterer()
        await plugin.start({})
        c1 = _cell([1.0, 0.0])
        c2 = _cell([1.0, 0.0])  # 同向量但不同 group
        cid1, _ = await plugin.cluster("g1", c1)
        cid2, meta2 = await plugin.cluster("g2", c2)
        assert meta2.is_new_cluster is True  # 跨 group 隔离
        assert cid1 != cid2

    async def test_lru_eviction(self):
        plugin = IncrementalCentroidClusterer()
        await plugin.start({"max_scenes_per_group": 2})
        # 三个正交 cell → 三个新 scene → 第一个被淘汰
        # 但由于按 dict insertion order 弹出最早,刚插入的不会立刻丢
        c_a = _cell([1.0, 0.0, 0.0])
        c_b = _cell([0.0, 1.0, 0.0])
        c_c = _cell([0.0, 0.0, 1.0])
        await plugin.cluster("g", c_a)
        await plugin.cluster("g", c_b)
        await plugin.cluster("g", c_c)
        scenes = plugin.get_scenes("g", "t1", "u1")
        assert len(scenes) == 2  # 上限 2

    async def test_empty_embedding_creates_solo(self):
        plugin = IncrementalCentroidClusterer()
        await plugin.start({})
        cell = _cell(None)
        cid, meta = await plugin.cluster("g", cell)
        assert meta.is_new_cluster is True

    async def test_health_metrics(self):
        plugin = IncrementalCentroidClusterer()
        await plugin.start({})
        await plugin.cluster("g", _cell([1.0, 0.0]))
        h = await plugin.health()
        assert h["status"] == "ok"
        m = await plugin.metrics()
        assert m["incremental_centroid_scenes"] == 1
