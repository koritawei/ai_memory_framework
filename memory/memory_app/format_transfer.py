"""外部 ``MemoryIngestRequest`` → 内部 ``RawData`` 列表的转换层。

═══════════════════════════════════════════════════════════════════════════════
两种粒度
═══════════════════════════════════════════════════════════════════════════════
- :func:`ingest_to_raw_data_list`           一个 HistorySession → 一个 RawData
                                            （session 粒度，默认）
- :func:`ingest_to_raw_data_list_per_turn`  一个 ConversationTurn → 一个 RawData
                                            （turn 粒度，EverMemOS 风格）

写入热路径默认使用 session 粒度；如需细粒度切边界，可在
SBD 阶段切到 turn 粒度。

═══════════════════════════════════════════════════════════════════════════════
转换字段映射
═══════════════════════════════════════════════════════════════════════════════
| 外部字段                                  | 内部字段              |
| ----------------------------------------- | --------------------- |
| ``MemoryIngestRequest.tenant_id``         | ``RawData.tenant_id`` |
| ``MemoryIngestRequest.user_id``           | ``RawData.user_id``   |
| ``MemoryIngestRequest.event_time``        | ``RawData.event_time``|
| ``MemoryIngestRequest.raw_data_type``     | ``RawData.raw_data_type`` |
| ``MemoryIngestRequest.metadata``          | ``RawData.metadata``  |
| ``HistorySession.session_id``             | ``RawData.session_id``|
| ``HistorySession.session_date``           | 写入 ``metadata`` 便于 SBD 时间窗 |
| ``ConversationTurn.role`` + ``content``   | ``RawData.content`` 拼接 |
| ``ConversationTurn.dia_id`` (LoCoMo)      | 写入 ``metadata`` 用作溯源 |
| ``HistorySession.speaker_a/b``            | 写入 ``metadata`` 多说话人记录 |
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from memory_app.internal_models import RawData
from memory_app.schemas.ingest import (
    ConversationTurn,
    HistorySession,
    MemoryIngestRequest,
)


# ════════════════════════════════════════════════════════════════════════════
# 默认：session 粒度
# ════════════════════════════════════════════════════════════════════════════
def ingest_to_raw_data_list(request: MemoryIngestRequest) -> list[RawData]:
    """每个 :class:`HistorySession` 生成一个 :class:`RawData`。

    每 session 的 turns 按 ``role: content`` 格式拼为多行文本。
    适用于：SBD 在 session 边界天然切片的场景（写入热路径默认）。

    :returns: ``list[RawData]``，长度等于 ``request.history_sessions`` 长度
    """
    results: list[RawData] = []
    request_meta_base = _build_request_metadata(request)

    for session in request.history_sessions:
        content = _join_turns(session.turns)
        metadata = _merge_session_metadata(request_meta_base, session, request)

        results.append(
            RawData(
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                session_id=session.session_id,
                raw_data_type=request.raw_data_type.value,
                content=content,
                event_time=session.session_date or session.session_start or request.event_time,
                metadata=metadata,
            )
        )

    return results


# ════════════════════════════════════════════════════════════════════════════
# 进阶：turn 粒度（对齐 ）
# ════════════════════════════════════════════════════════════════════════════
def ingest_to_raw_data_list_per_turn(request: MemoryIngestRequest) -> list[RawData]:
    """每个 :class:`ConversationTurn` 生成一个 :class:`RawData`。

    每条 RawData 仅承载单轮原文。适用于：
    - SBD 在 turn 级做细粒度边界检测（ EverMemOS 风格）
    - 评测时需要精确到 dia_id 级溯源（LoCoMo）

    :returns: ``list[RawData]``，长度等于所有 ``turns`` 累加
    """
    results: list[RawData] = []
    request_meta_base = _build_request_metadata(request)

    for session in request.history_sessions:
        for turn in session.turns:
            metadata = _merge_session_metadata(request_meta_base, session, request)
            metadata.update(_build_turn_metadata(turn))

            #: 单 turn content：``role: text`` 保留对话角色信息，
            #: 便于下游 SBD / EpisodeExtractor 不丢失说话人语境
            content = f"{turn.role.value}: {turn.content}"

            results.append(
                RawData(
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    session_id=session.session_id,
                    raw_data_type=request.raw_data_type.value,
                    content=content,
                    event_time=turn.timestamp
                    or session.session_date
                    or session.session_start
                    or request.event_time,
                    metadata=metadata,
                )
            )

    return results


# ════════════════════════════════════════════════════════════════════════════
# 内部辅助
# ════════════════════════════════════════════════════════════════════════════
def _join_turns(turns: list[ConversationTurn]) -> str:
    """把多条 turn 按 ``role: content`` 格式拼接为多行文本块。

    保留对话角色让下游 SBD / LLM 抽取仍能识别说话人轮换。
    """
    lines = []
    for t in turns:
        # speaker_name（多 Agent 场景）优先于 role
        speaker = t.speaker_name or t.role.value
        lines.append(f"{speaker}: {t.content}")
    return "\n".join(lines)


def _build_request_metadata(request: MemoryIngestRequest) -> dict[str, Any]:
    """从 request 顶层提取要透传给 RawData.metadata 的字段。"""
    meta: dict[str, Any] = {}
    # 客户端传入的业务 metadata 优先保留
    if request.metadata:
        meta.update(request.metadata)
    # 业务标识：agent / adiu 帮助下游做多 Agent 隔离
    if request.agent_id:
        meta["agent_id"] = request.agent_id
    if request.agent_role:
        meta["agent_role"] = request.agent_role
    if request.adiu:
        meta["adiu"] = request.adiu
    if request.session_id:
        # 顶层 session_id（当前 ingest 调用关联）；不同于 HistorySession.session_id
        meta["request_session_id"] = request.session_id
    # 写入幂等键传递给下游 SBD 做去重
    if request.idempotency_key:
        meta["idempotency_key"] = request.idempotency_key
    # 显式区分"未设置"和"设置为 0":0 也是合法的一致性级别取值
    if request.min_read_consistency is not None:
        meta["min_read_consistency"] = request.min_read_consistency
    return meta


def _merge_session_metadata(
    base: dict[str, Any], session: HistorySession, request: MemoryIngestRequest
) -> dict[str, Any]:
    """把 session 级字段合并入 base metadata。"""
    meta = dict(base)  # 浅拷贝避免污染
    if session.speaker_a:
        meta["speaker_a"] = session.speaker_a
    if session.speaker_b:
        meta["speaker_b"] = session.speaker_b
    if session.session_summary:
        meta["session_summary"] = session.session_summary
    if session.session_observation:
        meta["session_observation"] = session.session_observation
    if session.extra:
        meta["session_extra"] = session.extra
    return meta


def _build_turn_metadata(turn: ConversationTurn) -> dict[str, Any]:
    """从单 turn 提取要写入 metadata 的标注字段。"""
    meta: dict[str, Any] = {}
    if turn.dia_id:
        meta["dia_id"] = turn.dia_id  # LoCoMo 证据链 ID
    if turn.turn_index is not None:
        meta["turn_index"] = turn.turn_index
    if turn.has_answer:
        meta["has_answer"] = turn.has_answer  # LongMemEval 评测标注
    if turn.token_count is not None:
        meta["token_count"] = turn.token_count
    if turn.img_url:
        meta["img_url"] = turn.img_url
    if turn.caption:
        meta["caption"] = turn.caption
    if turn.extra:
        meta["turn_extra"] = turn.extra
    return meta


__all__ = ["ingest_to_raw_data_list", "ingest_to_raw_data_list_per_turn"]
