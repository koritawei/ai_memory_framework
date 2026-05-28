"""SemanticMemoryExtractor —— 从 EpisodicMemory 联想 SemanticMemory。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
本模块承载 LLM 语义记忆联想的**核心算法**。插件层
:class:`memory_app.plugins_default.llm_10_association.LLM10AssociationExtractor`
是薄包装,负责满足 :class:`SemanticExtractor` SPI(start/stop + extract_for_*)。

策略:对每条 EpisodicMemory 调一次 LLM,prompt=``semantic_extraction``,
LLM 应返回多条 SemanticMemory dict。"10 联想"指**期望产出条数上限**约 10
(由 prompt 引导,实际可在 [3, 20] 之间)。

═══════════════════════════════════════════════════════════════════════════════
失败语义
═══════════════════════════════════════════════════════════════════════════════
- LLM 解析失败 → 返回空列表(冷路径不阻塞,后续巩固阶段可再次尝试)
- LLM 调用异常 → 抛原异常(由插件层包装为 PluginError)
- 空 LLM 响应("[]") → 返回空列表
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from memory_app.internal_models import (
    EpisodicMemory,
    KnowledgeType,
    MemCell,
    SemanticMemory,
)
from memory_app.prompt_runtime import get_prompt_manager

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 核心抽取器
# ════════════════════════════════════════════════════════════════════════════
class SemanticMemoryExtractor:
    """纯算法语义抽取器。

    构造参数:
        ``llm_client``       任何 ``await llm.generate(prompt) -> str`` 的对象
        ``prompt_id``        默认 ``semantic_extraction``
        ``min_items``/``max_items``  截断防止 LLM 失控(默认 0/20;实际 LLM 难以稳定 10 条)
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        prompt_id: str = "semantic_extraction",
        min_items: int = 0,
        max_items: int = 20,
    ) -> None:
        self.llm_client = llm_client
        self._prompt_id = prompt_id
        self._min_items = max(0, min_items)
        self._max_items = max(self._min_items, max_items)

    # ────────────────────────────────────────────────────────────────────────
    # Public API
    # ────────────────────────────────────────────────────────────────────────
    async def extract(self, episode: EpisodicMemory) -> list[SemanticMemory]:
        """从单条 EpisodicMemory 联想出多条 SemanticMemory。"""
        if not episode.summary or not episode.summary.strip():
            return []
        prompt = await self._render_prompt(
            tenant_id=episode.tenant_id,
            user_id=episode.user_id,
            summary=episode.summary,
            entities=", ".join(episode.key_entities),
        )
        # LLM 调用异常上抛,由插件层包装
        response = await self.llm_client.generate(prompt)
        items = parse_semantic_response(response)
        items = items[: self._max_items]
        return [
            self._to_semantic_from_episode(episode, it)
            for it in items
        ]

    async def extract_for_memcell(self, memcell: MemCell) -> list[SemanticMemory]:
        """从 MemCell 直接联想(SBD 阶段调用)。

        prompt 输入 ``summary=memcell.summary or memcell.text``。
        """
        text = memcell.summary or memcell.text or ""
        if not text.strip():
            return []
        prompt = await self._render_prompt(
            tenant_id=memcell.tenant_id,
            user_id=memcell.user_id,
            summary=text,
            entities="",
        )
        response = await self.llm_client.generate(prompt)
        items = parse_semantic_response(response)
        items = items[: self._max_items]
        return [
            self._to_semantic_from_memcell(memcell, it)
            for it in items
        ]

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    async def _render_prompt(
        self, *, tenant_id: str, user_id: str, summary: str, entities: str
    ) -> str:
        try:
            return await get_prompt_manager().render_for(
                self._prompt_id,
                tenant_id=tenant_id,
                user_id=user_id,
                summary=summary,
                entities=entities,
            )
        except Exception as e:  # noqa: BLE001
            # 模板缺失 / 变量校验失败 → 兜底
            logger.warning("semantic prompt render failed (%s): %s", self._prompt_id, e)
            return (
                "从以下情景与实体归纳语义记忆,返回 JSON 数组:\n"
                f"摘要:{summary}\n实体:{entities}"
            )

    @staticmethod
    def _to_semantic_from_episode(
        episode: EpisodicMemory, item: dict[str, Any]
    ) -> SemanticMemory:
        return SemanticMemory(
            tenant_id=episode.tenant_id,
            user_id=episode.user_id,
            content=str(item.get("content") or "").strip(),
            knowledge_type=_to_knowledge_type(item.get("knowledge_type")),
            source_episode_ids=[episode.episode_id],
            source_memcell_ids=[episode.mem_cell_id] if episode.mem_cell_id else [],
            confidence=_to_float(item.get("confidence"), 0.8, lo=0.0, hi=1.0),
            start_time=_optional_str(item.get("start_time")),
            end_time=_optional_str(item.get("end_time")),
            duration_days=_optional_int(item.get("duration_days")),
        )

    @staticmethod
    def _to_semantic_from_memcell(
        memcell: MemCell, item: dict[str, Any]
    ) -> SemanticMemory:
        return SemanticMemory(
            tenant_id=memcell.tenant_id,
            user_id=memcell.user_id,
            content=str(item.get("content") or "").strip(),
            knowledge_type=_to_knowledge_type(item.get("knowledge_type")),
            source_episode_ids=[],
            source_memcell_ids=[memcell.mem_cell_id],
            confidence=_to_float(item.get("confidence"), 0.8, lo=0.0, hi=1.0),
            start_time=_optional_str(item.get("start_time")),
            end_time=_optional_str(item.get("end_time")),
            duration_days=_optional_int(item.get("duration_days")),
        )


# ════════════════════════════════════════════════════════════════════════════
# 解析
# ════════════════════════════════════════════════════════════════════════════
def parse_semantic_response(response: str) -> list[dict[str, Any]]:
    """容错解析 LLM 语义抽取响应,过滤掉 ``content`` 为空的脏条目。"""
    if not response or not response.strip():
        return []
    text = _strip_code_fence(response.strip())
    obj: Any = None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
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
        obj = [obj]
    if not isinstance(obj, list):
        return []
    out: list[dict[str, Any]] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        out.append(item)
    return out


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
_CODE_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    m = _CODE_FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text


def _to_knowledge_type(value: Any) -> KnowledgeType:
    if not value:
        return KnowledgeType.KNOWLEDGE
    try:
        return KnowledgeType(str(value).strip().lower())
    except ValueError:
        return KnowledgeType.KNOWLEDGE


def _to_float(value: Any, default: float, *, lo: float, hi: float) -> float:
    try:
        f = float(value) if value is not None else default
    except (TypeError, ValueError):
        f = default
    return max(lo, min(hi, f))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "SemanticMemoryExtractor",
    "parse_semantic_response",
]
