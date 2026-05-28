""" 验收：外部 Pydantic 契约。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memory_app.schemas import (
    ConversationTurn,
    FeedbackRequest,
    FeedbackType,
    HistorySession,
    MemoryIngestRequest,
    RawDataType,
    RetrieveMemRequest,
    RetrievalIntent,
    RoleEnum,
)


# ─────────────────────────────────────────────────────────────────────────────
#  范例 1：LongMemEval 风格（导航通勤场景）
# ─────────────────────────────────────────────────────────────────────────────
LONGMEMEVAL_EXAMPLE = {
    "tenant_id": "amap",
    "user_id": "user001",
    "agent_id": "navigator_v1",
    "agent_role": "navigator",
    "adiu": "amap-ios-nav-user001",
    "session_id": "sess_query_001",
    "event_time": "2026-05-12T10:00:00Z",
    "history_sessions": [
        {
            "session_id": "haystack_001",
            "session_date": "2026-04-20T08:30:00Z",
            "turns": [
                {"role": "user", "content": "帮我导航到公司", "turn_index": 0},
                {
                    "role": "assistant",
                    "content": "好的，推荐走机场高速，预计35分钟",
                    "turn_index": 1,
                    "has_answer": True,
                },
            ],
        },
        {
            "session_id": "haystack_002",
            "session_date": "2026-04-21T08:25:00Z",
            "turns": [
                {"role": "user", "content": "今天还是走机场高速吧", "turn_index": 0},
                {"role": "assistant", "content": "收到", "turn_index": 1},
            ],
        },
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
#  范例 2：LoCoMo 风格（多说话人 + dia_id）
# ─────────────────────────────────────────────────────────────────────────────
LOCOMO_EXAMPLE = {
    "tenant_id": "locomo",
    "user_id": "user002",
    "agent_id": "assistant_v2",
    "history_sessions": [
        {
            "session_id": "D1",
            "session_date": "2023-05-07T14:00:00Z",
            "speaker_a": "Caroline",
            "speaker_b": "Melanie",
            "turns": [
                {
                    "role": "user",
                    "content": "I went to the LGBTQ support group today.",
                    "turn_index": 0,
                    "dia_id": "D1:3",
                    "speaker_name": "Caroline",
                },
                {
                    "role": "assistant",
                    "content": "That's great! How was it?",
                    "turn_index": 1,
                    "dia_id": "D1:4",
                    "speaker_name": "Melanie",
                },
            ],
        }
    ],
}


# ── MemoryIngestRequest 解析 ──
def test_parse_longmemeval_example():
    req = MemoryIngestRequest.model_validate(LONGMEMEVAL_EXAMPLE)
    assert req.tenant_id == "amap"
    assert req.user_id == "user001"
    assert req.agent_role == "navigator"
    assert req.adiu == "amap-ios-nav-user001"
    assert len(req.history_sessions) == 2
    assert len(req.history_sessions[0].turns) == 2
    assert req.history_sessions[0].turns[1].has_answer is True
    assert req.raw_data_type == RawDataType.CONVERSATION


def test_parse_locomo_example():
    req = MemoryIngestRequest.model_validate(LOCOMO_EXAMPLE)
    s = req.history_sessions[0]
    assert s.speaker_a == "Caroline"
    assert s.speaker_b == "Melanie"
    t0 = s.turns[0]
    assert t0.dia_id == "D1:3"
    assert t0.speaker_name == "Caroline"


def test_missing_tenant_id_raises():
    bad = {**LONGMEMEVAL_EXAMPLE}
    del bad["tenant_id"]
    with pytest.raises(ValidationError):
        MemoryIngestRequest.model_validate(bad)


def test_missing_user_id_raises():
    bad = {**LONGMEMEVAL_EXAMPLE}
    del bad["user_id"]
    with pytest.raises(ValidationError):
        MemoryIngestRequest.model_validate(bad)


def test_role_enum_invalid():
    with pytest.raises(ValidationError):
        ConversationTurn(role="invalid_role", content="x")


def test_extra_field_forbidden_on_request():
    bad = {**LONGMEMEVAL_EXAMPLE, "unknown_field": "rogue"}
    with pytest.raises(ValidationError):
        MemoryIngestRequest.model_validate(bad)


# ── HistorySession ──
def test_session_with_no_turns_is_ok():
    """空 session（仅 session_summary 摘要）也应能合法。"""
    s = HistorySession(session_id="s1", turns=[], session_summary="empty")
    assert s.turns == []


# ── RetrieveMemRequest ──
def test_retrieve_request_defaults():
    req = RetrieveMemRequest(tenant_id="t", user_id="u", query="北京")
    assert req.top_k == 10
    assert req.intent == RetrievalIntent.AUTO
    assert req.enable_graph is False
    assert req.debug is False


def test_retrieve_request_top_k_bounds():
    with pytest.raises(ValidationError):
        RetrieveMemRequest(tenant_id="t", user_id="u", query="x", top_k=0)
    with pytest.raises(ValidationError):
        RetrieveMemRequest(tenant_id="t", user_id="u", query="x", top_k=999)


# ── FeedbackRequest ──
def test_feedback_default_signal():
    fb = FeedbackRequest(
        tenant_id="t", user_id="u", mem_cell_id="m", feedback_type=FeedbackType.POSITIVE
    )
    assert fb.signal_value == 0.0


def test_feedback_supports_explicit_signal():
    fb = FeedbackRequest(
        tenant_id="t",
        user_id="u",
        memory_id="mem-x",
        feedback_type=FeedbackType.CORRECTION,
        signal_value=-2.0,
    )
    assert fb.signal_value == -2.0


# ── 序列化往返 ──
def test_round_trip_serialization():
    req = MemoryIngestRequest.model_validate(LONGMEMEVAL_EXAMPLE)
    j = req.model_dump_json()
    assert "amap" in j
    req2 = MemoryIngestRequest.model_validate_json(j)
    assert req2.user_id == req.user_id
