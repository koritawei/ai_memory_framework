""" 验收：内部数据模型。"""

from __future__ import annotations

from datetime import datetime

import pytest

from memory_app.internal_models import (
    AccessControlMeta,
    ConsolidationStatus,
    EpisodicMemory,
    EventLog,
    KnowledgeType,
    LifecycleMeta,
    MemCell,
    MemoryState,
    MemoryType,
    MemScene,
    MetaMemory,
    ProvenanceMeta,
    RankedMemory,
    RawData,
    SemanticMemory,
)


# ── 默认值与 UUID 自动生成 ──
def test_memcell_defaults():
    mc = MemCell(tenant_id="t1", user_id="u1", session_id="s1", text="test")
    assert mc.state == MemoryState.ACTIVE
    assert mc.strength == 1.0
    assert mc.access_count == 0
    assert mc.consolidation_status == ConsolidationStatus.PENDING
    # UUID 自动生成且非空
    assert isinstance(mc.mem_cell_id, str) and len(mc.mem_cell_id) >= 36


def test_memcell_uuids_distinct():
    a = MemCell(tenant_id="t", user_id="u", session_id="s", text="a")
    b = MemCell(tenant_id="t", user_id="u", session_id="s", text="b")
    assert a.mem_cell_id != b.mem_cell_id


# ── RawData ──
def test_rawdata_creation():
    rd = RawData(
        tenant_id="t",
        user_id="u",
        session_id="s",
        content="hello",
        event_time=datetime(2026, 1, 1),
    )
    assert rd.raw_data_type == "CONVERSATION"
    assert rd.metadata == {}


# ── EpisodicMemory ──
def test_episodic_memory_defaults():
    e = EpisodicMemory(mem_cell_id="m1", tenant_id="t", user_id="u", summary="abc")
    assert e.state == MemoryState.ACTIVE
    assert e.emotional_valence == 0.0
    assert e.key_entities == []


# ── SemanticMemory ──
def test_semantic_memory_with_knowledge_type():
    s = SemanticMemory(
        tenant_id="t",
        user_id="u",
        content="用户偏好机场高速",
        knowledge_type=KnowledgeType.PREFERENCE,
    )
    assert s.knowledge_type == KnowledgeType.PREFERENCE
    assert s.is_valid is True
    assert s.evidence_count == 1


def test_semantic_memory_supersede_marks_invalid():
    """模拟 Consolidator SUPERSEDE：旧事实标记 is_valid=false。"""
    old = SemanticMemory(tenant_id="t", user_id="u", content="Alice 住北京")
    old.is_valid = False
    assert old.is_valid is False


# ── MemScene ──
def test_memscene_defaults():
    sc = MemScene(tenant_id="t", user_id="u")
    assert sc.member_count == 0
    assert sc.pending_semantic_digest is True
    assert sc.member_episode_ids == []
    assert sc.consolidated_semantic_ids == []


# ── EventLog ──
def test_event_log_arrays_consistency():
    """约定：atomic_facts 与 fact_embeddings 一一对应。"""
    el = EventLog(
        tenant_id="t",
        user_id="u",
        time="March 10, 2024 at 2:00 PM",
        atomic_facts=["a", "b"],
        fact_embeddings=[[0.1] * 4, [0.2] * 4],
    )
    assert len(el.atomic_facts) == len(el.fact_embeddings)


# ── MetaMemory 三子结构组合 ──
def test_meta_memory_composition():
    mm = MetaMemory(
        tenant_id="t",
        user_id="u",
        target_memory_id="ep-1",
        target_memory_type=MemoryType.EPISODIC,
    )
    assert isinstance(mm.provenance, ProvenanceMeta)
    assert isinstance(mm.lifecycle, LifecycleMeta)
    assert isinstance(mm.access_control, AccessControlMeta)
    assert mm.lifecycle.lifecycle == MemoryState.ACTIVE
    assert mm.access_control.visibility_scope == "private"


# ── RankedMemory ──
def test_ranked_memory_with_source_channel():
    rm = RankedMemory(
        memory_id="m1",
        memory_type=MemoryType.EPISODIC,
        content="文本",
        score=0.8,
        rank=0,
        source_channel="bm25",
    )
    assert rm.source_channel == "bm25"
    assert rm.memory_type == MemoryType.EPISODIC


# ── 枚举值校验 ──
def test_enum_values():
    assert MemoryState.ACTIVE.value == "ACTIVE"
    assert MemoryState.WARM.value == "WARM"
    assert MemoryState.COLD.value == "COLD"
    assert MemoryState.ARCHIVED.value == "ARCHIVED"
    assert KnowledgeType.PREFERENCE.value == "preference"
    assert ConsolidationStatus.DONE.value == "done"
    assert MemoryType.EPISODIC.value == "EPISODIC"


def test_state_invalid_value_raises():
    with pytest.raises(Exception):
        MemCell(tenant_id="t", user_id="u", session_id="s", text="x", state="HOT")  # type: ignore[arg-type]
