"""Step 1.3 验收：format_transfer 转换层。"""

from __future__ import annotations

from memory_app.format_transfer import (
    ingest_to_raw_data_list,
    ingest_to_raw_data_list_per_turn,
)
from memory_app.schemas.ingest import MemoryIngestRequest


# ─────────────────────────────────────────────────────────────────────────────
# session 粒度（默认）
# ─────────────────────────────────────────────────────────────────────────────
def test_single_session_session_level():
    req = MemoryIngestRequest.model_validate(
        {
            "tenant_id": "t1",
            "user_id": "u1",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [
                        {"role": "user", "content": "你好"},
                        {"role": "assistant", "content": "你好！"},
                    ],
                }
            ],
        }
    )
    raw_list = ingest_to_raw_data_list(req)
    assert len(raw_list) == 1
    assert raw_list[0].session_id == "s1"
    # role: content 拼接后含两行
    assert "user: 你好" in raw_list[0].content
    assert "assistant: 你好！" in raw_list[0].content
    assert raw_list[0].tenant_id == "t1"
    assert raw_list[0].user_id == "u1"


def test_multi_session():
    req = MemoryIngestRequest.model_validate(
        {
            "tenant_id": "t",
            "user_id": "u",
            "history_sessions": [
                {"session_id": "s1", "turns": [{"role": "user", "content": "a"}]},
                {"session_id": "s2", "turns": [{"role": "user", "content": "b"}]},
            ],
        }
    )
    raw_list = ingest_to_raw_data_list(req)
    assert len(raw_list) == 2
    assert {r.session_id for r in raw_list} == {"s1", "s2"}


def test_metadata_propagates_request_fields():
    """request 顶层字段应进入 RawData.metadata 便于下游溯源。"""
    req = MemoryIngestRequest.model_validate(
        {
            "tenant_id": "t",
            "user_id": "u",
            "agent_id": "agent-x",
            "agent_role": "navigator",
            "adiu": "amap-ios",
            "metadata": {"client": "mobile"},
            "history_sessions": [
                {"session_id": "s1", "turns": [{"role": "user", "content": "hi"}]}
            ],
        }
    )
    raw = ingest_to_raw_data_list(req)[0]
    assert raw.metadata["agent_id"] == "agent-x"
    assert raw.metadata["agent_role"] == "navigator"
    assert raw.metadata["adiu"] == "amap-ios"
    assert raw.metadata["client"] == "mobile"


def test_speaker_fields_propagate():
    req = MemoryIngestRequest.model_validate(
        {
            "tenant_id": "t",
            "user_id": "u",
            "history_sessions": [
                {
                    "session_id": "D1",
                    "speaker_a": "Alice",
                    "speaker_b": "Bob",
                    "turns": [{"role": "user", "content": "hi", "speaker_name": "Alice"}],
                }
            ],
        }
    )
    raw = ingest_to_raw_data_list(req)[0]
    # speaker_name 优先于 role
    assert "Alice: hi" in raw.content
    assert raw.metadata["speaker_a"] == "Alice"
    assert raw.metadata["speaker_b"] == "Bob"


def test_session_date_used_as_event_time():
    req = MemoryIngestRequest.model_validate(
        {
            "tenant_id": "t",
            "user_id": "u",
            "event_time": "2026-05-12T10:00:00Z",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "session_date": "2026-04-20T08:30:00Z",  # 应优先于 request.event_time
                    "turns": [{"role": "user", "content": "x"}],
                }
            ],
        }
    )
    raw = ingest_to_raw_data_list(req)[0]
    assert raw.event_time.isoformat().startswith("2026-04-20")


def test_empty_sessions_returns_empty_list():
    req = MemoryIngestRequest(tenant_id="t", user_id="u", history_sessions=[])
    assert ingest_to_raw_data_list(req) == []


# ─────────────────────────────────────────────────────────────────────────────
# turn 粒度
# ─────────────────────────────────────────────────────────────────────────────
def test_turn_level_one_per_turn():
    req = MemoryIngestRequest.model_validate(
        {
            "tenant_id": "t",
            "user_id": "u",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [
                        {"role": "user", "content": "a", "dia_id": "D1:1", "turn_index": 0},
                        {"role": "assistant", "content": "b", "dia_id": "D1:2", "turn_index": 1},
                    ],
                }
            ],
        }
    )
    raw_list = ingest_to_raw_data_list_per_turn(req)
    assert len(raw_list) == 2
    # dia_id 入 metadata
    assert raw_list[0].metadata["dia_id"] == "D1:1"
    assert raw_list[1].metadata["dia_id"] == "D1:2"
    # turn_index 入 metadata
    assert raw_list[0].metadata["turn_index"] == 0


def test_turn_level_total_count_across_sessions():
    req = MemoryIngestRequest.model_validate(
        {
            "tenant_id": "t",
            "user_id": "u",
            "history_sessions": [
                {"session_id": "s1", "turns": [
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                    {"role": "user", "content": "c"},
                ]},
                {"session_id": "s2", "turns": [
                    {"role": "user", "content": "d"},
                ]},
            ],
        }
    )
    raw_list = ingest_to_raw_data_list_per_turn(req)
    assert len(raw_list) == 4


def test_turn_level_has_answer_propagates():
    req = MemoryIngestRequest.model_validate(
        {
            "tenant_id": "t",
            "user_id": "u",
            "history_sessions": [
                {
                    "session_id": "s1",
                    "turns": [
                        {"role": "user", "content": "q"},
                        {"role": "assistant", "content": "a", "has_answer": True},
                    ],
                }
            ],
        }
    )
    raw_list = ingest_to_raw_data_list_per_turn(req)
    assert raw_list[0].metadata.get("has_answer", False) is False
    assert raw_list[1].metadata["has_answer"] is True
