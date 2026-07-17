"""测试方案 §2.2 样本数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def minimal_ingest_body(*, tenant_id: str = "t1", user_id: str = "u1") -> dict:
    """最小对话样本。"""
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "history_sessions": [
            {
                "session_id": "s1",
                "turns": [
                    {"role": "user", "content": "我下周要去北京出差"},
                    {"role": "assistant", "content": "好的，我帮您记下"},
                ],
            }
        ],
    }


def beijing_trip_ingest_body(*, tenant_id: str = "t1", user_id: str = "u1") -> dict:
    """E2E 主链路：出差 + 偏好 + 客户拜访（§6.2）。"""
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "history_sessions": [
            {
                "session_id": "trip-1",
                "turns": [
                    {"role": "user", "content": "我下周要去北京出差"},
                    {"role": "assistant", "content": "已记录您的出差计划"},
                ],
            },
            {
                "session_id": "pref-1",
                "turns": [
                    {"role": "user", "content": "我喜欢住靠近地铁的酒店"},
                ],
            },
            {
                "session_id": "client-1",
                "turns": [
                    {"role": "user", "content": "这次北京行要拜访 Acme 客户"},
                ],
            },
        ],
    }


def locomoto_ingest_body(*, tenant_id: str = "t1", user_id: str = "u1") -> dict:
    """LoCoMo 兼容字段样本（UT-SCHEMA-004）。"""
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "history_sessions": [
            {
                "session_id": "locomo-s1",
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_summary": "讨论周末计划",
                "turns": [
                    {
                        "role": "user",
                        "content": "周六去爬山吗？",
                        "dia_id": "D1:1",
                        "speaker_name": "Alice",
                    },
                    {
                        "role": "assistant",
                        "content": "可以，几点出发？",
                        "dia_id": "D1:2",
                        "speaker_name": "Bob",
                    },
                ],
            }
        ],
    }


def tenant_a_coffee_body() -> dict:
    return minimal_ingest_body(tenant_id="tenant_a", user_id="user_1") | {
        "history_sessions": [
            {
                "session_id": "a1",
                "turns": [{"role": "user", "content": "我喜欢咖啡"}],
            }
        ]
    }


def tenant_b_tea_body() -> dict:
    return minimal_ingest_body(tenant_id="tenant_b", user_id="user_1") | {
        "history_sessions": [
            {
                "session_id": "b1",
                "turns": [{"role": "user", "content": "我喜欢茶"}],
            }
        ]
    }


def time_gap_ingest_body(*, gap_minutes: int = 45) -> dict:
    """时间断层样本（UT-SBD-002）。"""
    t0 = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=gap_minutes)
    return {
        "tenant_id": "t1",
        "user_id": "u1",
        "history_sessions": [
            {
                "session_id": "gap-s1",
                "turns": [
                    {"role": "user", "content": "早上开会", "timestamp": t0.isoformat()},
                    {"role": "user", "content": "下午写报告", "timestamp": t1.isoformat()},
                ],
            }
        ],
    }
