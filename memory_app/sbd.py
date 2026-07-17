"""SBD 规则算法 + LLM 兜底辅助(设计文档 §5.1.2 / §5.1.3)。

═══════════════════════════════════════════════════════════════════════════════
模块分工
═══════════════════════════════════════════════════════════════════════════════
- 规则路径(纯函数 / 无状态)::func:`should_split` / :func:`detect_boundaries`
- LLM 兜底(Phase 3,异步):
  - :func:`needs_llm_refinement`   启发式判定是否需要兜底
  - :func:`format_numbered_segments` 把 segment 列表渲染为带行号文本(prompt 入参)
  - :func:`parse_llm_boundary_response` 解析 LLM JSON 响应

插件层 :class:`memory_app.plugins_default.rule_sbd.RuleSBD` /
:class:`memory_app.plugins_default.hybrid_sbd.HybridSBD` /
:class:`memory_app.plugins_default.llm_sbd.LLMSBD` 是本模块的薄包装,
负责满足 :class:`memory_app.plugins.spi.boundary_detector.BoundaryDetector` SPI;
真实算法在这里,Prompt 模板经 :mod:`memory_app.prompt_runtime` 解析,
**禁止**在本模块或插件内硬编码 prompt 字符串。

═══════════════════════════════════════════════════════════════════════════════
切分规则(优先级顺序,任一命中即切)
═══════════════════════════════════════════════════════════════════════════════
1. **时间间隔**:相邻消息时间差 > ``time_gap_threshold``(默认 30 min)
2. **窗口轮数**:当前 segment turns 数 ≥ ``max_window_turns``(默认 20)
3. **窗口 token**:当前 segment 累计字符 / 4 ≥ ``max_window_tokens``(默认 512)

═══════════════════════════════════════════════════════════════════════════════
Phase 3 LLM 兜底
═══════════════════════════════════════════════════════════════════════════════
``HybridSBD`` 先走规则得到候选 segment 列表;若任一 segment 过大或多样性高
(:func:`needs_llm_refinement`),把该 segment 编号化送入 LLM,LLM 返回
``boundary_index`` 后在该位置二次切分。LLM 失败 → 安全回退原规则结果。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from memory_app.internal_models import RawData

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class SBDConfig:
    """SBD 规则配置。

    所有阈值都可由 ConfigCenter 下发(见 plugins_default/rule_sbd.py)。
    """

    #: 相邻消息时间差超此值则切边界
    time_gap_threshold: timedelta = timedelta(minutes=30)

    #: 单 segment 最多累计 turn 数
    max_window_turns: int = 20

    #: 单 segment 最多累计 token(字符 / 4 近似)
    max_window_tokens: int = 512


# ════════════════════════════════════════════════════════════════════════════
# 单步判定
# ════════════════════════════════════════════════════════════════════════════
def should_split(
    current: list[RawData],
    incoming: RawData,
    config: SBDConfig,
) -> tuple[bool, str]:
    """对 ``incoming`` 是否相对 ``current`` 切边界做单步判定。

    :returns: ``(should_end, reasoning)``;``should_end=True`` 表示在
              ``incoming`` 之前切一刀,新建 segment。
    """
    if not current:
        # current 为空时无可比对象,不切
        return False, "cold_start"

    # 规则 1:时间间隔
    last_time = _normalize_time(current[-1].event_time)
    cur_time = _normalize_time(incoming.event_time)
    if cur_time - last_time > config.time_gap_threshold:
        return True, "time_gap_exceeded"

    # 规则 2:窗口 turns 数
    if len(current) >= config.max_window_turns:
        return True, "max_window_turns_reached"

    # 规则 3:token 近似
    total_chars = sum(len(r.content) for r in current)
    # 字符数 / 4 是粗略 token 数;CJK / 英文都偏保守
    approx_tokens = total_chars // 4
    if approx_tokens >= config.max_window_tokens:
        return True, "max_window_tokens_reached"

    return False, "within_window"


# ════════════════════════════════════════════════════════════════════════════
# 批量切分(消费方便利)
# ════════════════════════════════════════════════════════════════════════════
def detect_boundaries(
    raw_data_list: list[RawData],
    config: SBDConfig | None = None,
) -> list[list[RawData]]:
    """把 ``raw_data_list`` 按规则切分为多段 segment。

    适用于:
    - 离线评测重放:一次性传入完整 raws 切多段
    - 单元测试:验证规则正确性

    在线热路径走 :meth:`memory_app.plugins.spi.boundary_detector.BoundaryDetector.detect`
    单步增量判定。两者**共享同一规则函数** :func:`should_split`,行为一致。
    """
    if not raw_data_list:
        return []
    cfg = config or SBDConfig()

    segments: list[list[RawData]] = [[]]
    for rd in raw_data_list:
        cur = segments[-1]
        if not cur:
            cur.append(rd)
            continue
        end, _reason = should_split(cur, rd, cfg)
        if end:
            segments.append([rd])
        else:
            cur.append(rd)
    # 过滤空段(防御:cur 为空时不应切,但理论上不会发生)
    return [s for s in segments if s]


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def _normalize_time(t: datetime) -> datetime:
    """把 naive datetime 视为 UTC,避免时区比较抛 TypeError。"""
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t


def parse_sbd_config(params: dict[str, Any] | None) -> SBDConfig:
    """从插件参数 dict 构造 :class:`SBDConfig`。

    支持的字段(全部可选,缺失走默认值):
        - ``time_gap_min`` (int, 分钟):时间窗,转 timedelta
        - ``max_window_turns`` (int)
        - ``max_window_tokens`` (int)

    单字段解析失败时记 warning 并保留默认 —— 与 ``parse_fsfm_config`` /
    ``parse_reinforce_config`` 等其他配置解析器的错误处理风格一致,避免一个
    脏字段把整个插件 ``start()`` 拉崩。
    """
    cfg = SBDConfig()
    if not params:
        return cfg
    if "time_gap_min" in params:
        try:
            cfg.time_gap_threshold = timedelta(minutes=int(params["time_gap_min"]))
        except (TypeError, ValueError):
            logger.warning("invalid time_gap_min: %r", params["time_gap_min"])
    if "max_window_turns" in params:
        try:
            cfg.max_window_turns = int(params["max_window_turns"])
        except (TypeError, ValueError):
            logger.warning("invalid max_window_turns: %r", params["max_window_turns"])
    # 兼容 ``max_window_size``(noop_sbd 旧字段名)
    elif "max_window_size" in params:
        try:
            cfg.max_window_turns = int(params["max_window_size"])
        except (TypeError, ValueError):
            logger.warning("invalid max_window_size: %r", params["max_window_size"])
    if "max_window_tokens" in params:
        try:
            cfg.max_window_tokens = int(params["max_window_tokens"])
        except (TypeError, ValueError):
            logger.warning("invalid max_window_tokens: %r", params["max_window_tokens"])
    return cfg


# ════════════════════════════════════════════════════════════════════════════
# LLM 兜底辅助(Phase 3, Step 3.1)
# ════════════════════════════════════════════════════════════════════════════
#: 单 segment 超此 turns 数即触发 LLM 兜底(启发式默认值)
LLM_REFINE_TURNS_THRESHOLD = 10


def needs_llm_refinement(
    segments: list[list[RawData]],
    *,
    turns_threshold: int = LLM_REFINE_TURNS_THRESHOLD,
) -> bool:
    """启发式判定:是否需要 LLM 二次精细切分。

    触发条件(命中任一即返回 True):
    - 任一 segment turns 数 > ``turns_threshold``(默认 10)
    """
    for seg in segments:
        if len(seg) > turns_threshold:
            return True
    return False


def format_numbered_segments(segment: list[RawData]) -> str:
    """把单 segment 渲染为带行号文本,供 ``sbd_llm_refine`` prompt 消费。

    输出格式::

        [0] 第一条消息内容
        [1] 第二条消息内容
        ...

    `行号 == 在 segment 中的索引`,LLM 返回 ``boundary_index`` 即从该索引起切。
    """
    lines: list[str] = []
    for idx, rd in enumerate(segment):
        # 单行展示,内部换行被替换避免破坏行号锚
        text = (rd.content or "").replace("\n", " ").strip()
        lines.append(f"[{idx}] {text}")
    return "\n".join(lines)


def parse_llm_boundary_response(response: str) -> tuple[int, str, float]:
    """解析 LLM 返回的 JSON,提取 ``boundary_index`` / ``reasoning`` / ``confidence``。

    容错:
    - 非法 JSON / 缺字段 → 返回 ``(-1, "parse_failed", 0.0)``
    - ``boundary_index`` 取不到 int → 同上

    :returns: ``(boundary_index, reasoning, confidence)``。
              ``boundary_index == -1`` 表示"整段同一话题,不切分"。
    """
    fallback = (-1, "parse_failed", 0.0)
    if not response or not response.strip():
        return fallback
    text = response.strip()
    # 部分 LLM 会用 markdown ```json ... ``` 包裹;粗暴剥壳即可
    if text.startswith("```"):
        text = text.strip("`")
        # 去掉首行可能的 "json" 标识
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # 二级回退:在长文本中寻找首段 JSON object
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            obj = json.loads(text[start:end])
        except (ValueError, TypeError):
            return fallback
    if not isinstance(obj, dict):
        return fallback
    try:
        idx = int(obj.get("boundary_index", -1))
    except (TypeError, ValueError):
        return fallback
    reasoning = str(obj.get("reasoning", ""))
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return idx, reasoning, confidence


def split_segment_at(
    segment: list[RawData], boundary_index: int
) -> list[list[RawData]]:
    """在 ``segment`` 的 ``boundary_index`` 处一刀切。

    边界语义:``boundary_index`` 之前的归 segment[:idx],之后归 segment[idx:]。
    - ``idx <= 0`` 或 ``idx >= len(segment)`` → 不切,原样返回
    """
    if boundary_index <= 0 or boundary_index >= len(segment):
        return [list(segment)]
    return [list(segment[:boundary_index]), list(segment[boundary_index:])]


__all__ = [
    "SBDConfig",
    "should_split",
    "detect_boundaries",
    "parse_sbd_config",
    "LLM_REFINE_TURNS_THRESHOLD",
    "needs_llm_refinement",
    "format_numbered_segments",
    "parse_llm_boundary_response",
    "split_segment_at",
]
