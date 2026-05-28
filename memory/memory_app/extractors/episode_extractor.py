"""EpisodeMemoryExtractor —— 从 MemCell 中抽 EpisodicMemory。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
本模块承载 LLM 情景抽取的**核心算法**:
- 通过 :func:`get_prompt_manager.render_for(...)` 取 prompt(**禁止**硬编码)
- 调注入的 ``llm_client.generate(prompt)`` 取 LLM JSON
- :func:`parse_episode_response` 容错解析 → 转 :class:`EpisodicMemory` 列表
- 解析失败时退到"整段摘要"兜底,**不**抛异常

插件层 :class:`memory_app.plugins_default.llm_episode_extractor.LLMEpisodeExtractor`
继承本类,负责满足 :class:`EpisodeExtractor` SPI(start/stop/health)。

═══════════════════════════════════════════════════════════════════════════════
Prompt 选择
═══════════════════════════════════════════════════════════════════════════════
- ``ScenarioType.GROUP_CHAT`` → ``episode_extraction_group_chat``
- ``ScenarioType.ASSISTANT``  → ``episode_extraction``

实际 prompt_id 在构造时注入,默认按 scenario 走以上映射;运维可通过
ConfigCenter 把任意 prompt_id 替换。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from memory_app._compat import utcnow
from memory_app.internal_models import EpisodicMemory, MemCell
from memory_app.plugins.spi.episode_extractor import ScenarioType
from memory_app.prompt_runtime import get_prompt_manager

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Prompt 默认映射
# ════════════════════════════════════════════════════════════════════════════
DEFAULT_PROMPT_ID_BY_SCENARIO: dict[ScenarioType, str] = {
    ScenarioType.ASSISTANT: "episode_extraction",
    ScenarioType.GROUP_CHAT: "episode_extraction_group_chat",
}


# ════════════════════════════════════════════════════════════════════════════
# 核心抽取器
# ════════════════════════════════════════════════════════════════════════════
class EpisodeMemoryExtractor:
    """纯算法情景抽取器(可独立于插件框架使用)。

    构造参数:
        ``llm_client``         任何 ``await llm.generate(prompt) -> str`` 的对象
        ``prompt_id_assistant`` 个人助手场景 prompt_id(默认 ``episode_extraction``)
        ``prompt_id_group``    群聊场景 prompt_id(默认 ``episode_extraction_group_chat``)
        ``default_scenario``   未指定时的兜底 scenario(默认 ``GROUP_CHAT``)
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        prompt_id_assistant: str = "episode_extraction",
        prompt_id_group: str = "episode_extraction_group_chat",
        default_scenario: ScenarioType = ScenarioType.GROUP_CHAT,
    ) -> None:
        self.llm_client = llm_client
        self._prompt_id_assistant = prompt_id_assistant
        self._prompt_id_group = prompt_id_group
        self._default_scenario = default_scenario

    # ────────────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────────────
    async def extract(
        self,
        cell: MemCell,
        scenario: ScenarioType | None = None,
    ) -> list[EpisodicMemory]:
        """从单个 MemCell 抽取 EpisodicMemory 列表。

        语义:
        - ``cell.text`` 为空 → 返回空列表
        - LLM 返回非法 JSON → 返回 1 条"整段兜底"情景(``summary=cell.text 前 50 字``)
        - LLM 调用异常 → 抛原异常(由调用方决定重试 / DLQ)
        """
        if not cell.text or not cell.text.strip():
            return []
        scen = scenario or self._default_scenario
        prompt_id = (
            self._prompt_id_group
            if scen == ScenarioType.GROUP_CHAT
            else self._prompt_id_assistant
        )
        try:
            prompt = await get_prompt_manager().render_for(
                prompt_id,
                tenant_id=cell.tenant_id,
                user_id=cell.user_id,
                text=cell.text,
                participants=", ".join(cell.participants) if cell.participants else "",
            )
        except Exception as e:  # noqa: BLE001
            # 模板缺失 / 变量校验失败 → 用兜底 prompt 直接传 cell.text
            logger.warning("episode prompt render failed (%s): %s", prompt_id, e)
            prompt = f"提取以下对话的情景记忆,返回 JSON 数组:\n{cell.text}"

        # llm 调用异常上抛(由 BackgroundTaskRunner / 调用方处理 retry)
        response = await self.llm_client.generate(prompt)
        items = parse_episode_response(response)
        if not items:
            # 解析失败兜底:仍产出 1 条情景,保证下游 SemanticExtractor / Cluster 有输入
            items = [
                {
                    "summary": _truncate(cell.text, 50),
                    "key_entities": [],
                    "emotional_valence": 0.0,
                    "importance": 0.0,
                    "event_time": None,
                }
            ]
        return [
            self._to_episode(cell, item, scen)
            for item in items
        ]

    # ────────────────────────────────────────────────────────────────────────
    # 转换
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _to_episode(
        cell: MemCell, item: dict[str, Any], scenario: ScenarioType
    ) -> EpisodicMemory:
        """把单条 LLM 输出 dict 转 :class:`EpisodicMemory`。"""
        summary = str(item.get("summary") or "").strip() or _truncate(cell.text, 50)
        return EpisodicMemory(
            mem_cell_id=cell.mem_cell_id,
            tenant_id=cell.tenant_id,
            user_id=cell.user_id,
            summary=summary,
            content=cell.text,
            subject=cell.subject,
            episode=cell.episode,
            key_entities=_to_str_list(item.get("key_entities")),
            emotional_valence=_to_float(item.get("emotional_valence"), 0.0, lo=-1.0, hi=1.0),
            emotional_salience=_optional_float(item.get("emotional_salience"), lo=0.0, hi=1.0),
            emotion_type=str(item["emotion_type"]) if item.get("emotion_type") else None,
            event_time=str(item["event_time"]) if item.get("event_time") else None,
            event_time_range=str(item["event_time_range"]) if item.get("event_time_range") else None,
            importance_score=_to_float(item.get("importance"), 0.0, lo=0.0, hi=1.0),
            timestamp=cell.timestamp or utcnow(),
        )


# ════════════════════════════════════════════════════════════════════════════
# 解析
# ════════════════════════════════════════════════════════════════════════════
def parse_episode_response(response: str) -> list[dict[str, Any]]:
    """容错解析 LLM 情景抽取响应,返回 dict 列表(单条响应也归一化为列表)。

    支持四类输入:
    1. 标准 JSON 数组         ``[{...}, {...}]``
    2. 单 dict                ``{...}``  → ``[{...}]``
    3. markdown 代码块包裹    ``"```json\\n[...]\\n```"``
    4. 长文本中夹带 JSON       前后含解释文字时,提取首段 JSON 子串

    解析失败 → 返回空列表(调用方走兜底逻辑)。
    """
    if not response or not response.strip():
        return []
    text = _strip_code_fence(response.strip())
    obj: Any = None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        # 二次回退:寻找首段 [ ... ] 或 { ... }
        for opener, closer in (("[", "]"), ("{", "}")):
            try:
                start = text.index(opener)
                end = text.rindex(closer) + 1
                obj = json.loads(text[start:end])
                break
            except (ValueError, TypeError):
                continue
    if obj is None:
        return []
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    return []


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
_CODE_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    m = _CODE_FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text


def _truncate(text: str, n: int) -> str:
    text = text.strip()
    return text[:n] if len(text) > n else text


def _to_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        # LLM 偶尔返回 "a, b, c" 字符串;按逗号切
        return [s.strip() for s in value.split(",") if s.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _to_float(value: Any, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    try:
        f = float(value) if value is not None else default
    except (TypeError, ValueError):
        f = default
    if lo is not None:
        f = max(lo, f)
    if hi is not None:
        f = min(hi, f)
    return f


def _optional_float(value: Any, *, lo: float | None = None, hi: float | None = None) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if lo is not None:
        f = max(lo, f)
    if hi is not None:
        f = min(hi, f)
    return f


__all__ = [
    "EpisodeMemoryExtractor",
    "parse_episode_response",
    "DEFAULT_PROMPT_ID_BY_SCENARIO",
]
