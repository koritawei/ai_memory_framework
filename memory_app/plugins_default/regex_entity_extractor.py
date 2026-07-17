"""``regex_entity_extractor`` —— Phase 7 默认 EntityExtractor 插件。

═══════════════════════════════════════════════════════════════════════════════
策略
═══════════════════════════════════════════════════════════════════════════════
轻量启发式实体抽取(Phase 7 入门级,不依赖 spaCy):
- 引号内文本                 → ``QUOTED``
- 连续大写英文词序列         → ``PROPER``
- 连续 CJK 字符段(≥ 2 字)   → ``COMPOUND``
- 单个长英文词(≥ 3 字)      → ``NOUN``

═══════════════════════════════════════════════════════════════════════════════
设计文档要求
═══════════════════════════════════════════════════════════════════════════════
- 必须按 ``normalized``(小写 / strip) 去重
- 子串实体应被丢弃("机场" 是 "首都机场" 子串则去掉前者)
- 过滤泛化词(thing / stuff / way / time)
- 单次调用 P50 < 50ms
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.entity_extractor import (
    Entity,
    EntityExtractor,
    EntityType,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 正则
# ════════════════════════════════════════════════════════════════════════════
_QUOTED_RE = re.compile(r'["「『“‘]([^"」』”’]{2,40})["」』”’]')
_PROPER_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:[\s-][A-Z][a-zA-Z]+)*)\b")
_CJK_RE = re.compile(r"[一-鿿]{2,8}")
_NOUN_EN_RE = re.compile(r"\b([a-zA-Z]{3,20})\b")

#: 通用低信息量词;命中即丢
DEFAULT_STOPWORDS = frozenset(
    {
        # 英文
        "the", "and", "but", "for", "with", "from", "into", "onto",
        "thing", "stuff", "way", "time", "today", "yesterday",
        "tomorrow", "user", "users", "memory", "memories",
        # 中文
        "今天", "昨天", "明天", "什么", "怎么", "可以", "需要",
        "用户", "我们", "你们", "他们", "这个", "那个", "一个",
        "时间", "事情", "东西", "问题", "情况",
    }
)


# ════════════════════════════════════════════════════════════════════════════
# 插件
# ════════════════════════════════════════════════════════════════════════════
@register
class RegexEntityExtractor(EntityExtractor):
    """正则启发式实体抽取(Phase 7 默认 fallback)。"""

    meta = PluginMeta(
        name="regex_entity_extractor",
        category="memory.generation.entity_extractor",
        version="1.0.0",
        description="正则 + 启发式实体抽取(无依赖,Phase 7 默认 fallback)",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "min_length": {"type": "integer", "minimum": 1, "maximum": 10, "default": 2},
                "max_entities_per_text": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 20
                },
                "extra_stopwords": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    )

    def __init__(self) -> None:
        self._min_length: int = 2
        self._max_entities: int = 20
        self._stopwords: set[str] = set(DEFAULT_STOPWORDS)

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._min_length = max(1, int(config.get("min_length", 2)))
        self._max_entities = max(1, int(config.get("max_entities_per_text", 20)))
        extra = config.get("extra_stopwords") or []
        self._stopwords = set(DEFAULT_STOPWORDS) | {
            str(w).strip().lower() for w in extra if str(w).strip()
        }
        logger.info(
            "regex_entity_extractor started: min_len=%d, max=%d, stopwords=%d",
            self._min_length, self._max_entities, len(self._stopwords),
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"min_len={self._min_length}, max={self._max_entities}, "
                f"stopwords={len(self._stopwords)}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # SPI:extract
    # ────────────────────────────────────────────────────────────────────────
    async def extract(self, text: str) -> list[Entity]:
        return self._extract_sync(text)

    async def extract_batch(self, texts: list[str]) -> list[list[Entity]]:
        return [self._extract_sync(t) for t in texts]

    # ────────────────────────────────────────────────────────────────────────
    # 同步实现(便于内联测试)
    # ────────────────────────────────────────────────────────────────────────
    def _extract_sync(self, text: str) -> list[Entity]:
        if not text or not text.strip():
            return []
        candidates: list[Entity] = []

        # 1. QUOTED
        for m in _QUOTED_RE.finditer(text):
            t = m.group(1).strip()
            if self._is_valid(t):
                candidates.append(Entity(
                    text=t, entity_type=EntityType.QUOTED,
                    normalized=_normalize(t), confidence=0.95,
                ))
        # 2. PROPER(英文专名)
        for m in _PROPER_RE.finditer(text):
            t = m.group(1).strip()
            if self._is_valid(t):
                candidates.append(Entity(
                    text=t, entity_type=EntityType.PROPER,
                    normalized=_normalize(t), confidence=0.85,
                ))
        # 3. COMPOUND(CJK 段)
        for m in _CJK_RE.finditer(text):
            t = m.group(0)
            if self._is_valid(t):
                candidates.append(Entity(
                    text=t, entity_type=EntityType.COMPOUND,
                    normalized=_normalize(t), confidence=0.75,
                ))
        # 4. NOUN(英文 fallback)
        for m in _NOUN_EN_RE.finditer(text):
            t = m.group(1).strip()
            if self._is_valid(t):
                candidates.append(Entity(
                    text=t, entity_type=EntityType.NOUN,
                    normalized=_normalize(t), confidence=0.55,
                ))

        # 去重 + 子串过滤(同 normalized 保留最高优先级 / 最长形式)
        deduped = self._dedupe_and_filter(candidates)
        return deduped[: self._max_entities]

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    def _is_valid(self, text: str) -> bool:
        text = text.strip()
        if len(text) < self._min_length:
            return False
        if _normalize(text) in self._stopwords:
            return False
        return True

    @staticmethod
    def _dedupe_and_filter(candidates: list[Entity]) -> list[Entity]:
        """同 normalized 仅保留最高优先级类型;子串实体被父串吸收。"""
        # 1. 同 normalized 取最高优先级类型(QUOTED > PROPER > COMPOUND > NOUN)
        priority = {
            EntityType.QUOTED: 0,
            EntityType.PROPER: 1,
            EntityType.COMPOUND: 2,
            EntityType.NOUN: 3,
        }
        by_norm: dict[str, Entity] = {}
        for e in candidates:
            key = e.normalized or e.text.lower()
            cur = by_norm.get(key)
            if cur is None or priority[e.entity_type] < priority[cur.entity_type]:
                by_norm[key] = e
        items = list(by_norm.values())
        # 2. 子串过滤:若 e1.normalized 是 e2.normalized 的真子串且类型相同 → 丢 e1
        items_sorted = sorted(
            items, key=lambda x: -len(x.normalized or x.text)
        )
        kept: list[Entity] = []
        for e in items_sorted:
            text_norm = (e.normalized or e.text).lower()
            is_substr = False
            for k in kept:
                kn = (k.normalized or k.text).lower()
                if text_norm != kn and text_norm in kn and e.entity_type == k.entity_type:
                    is_substr = True
                    break
            if not is_substr:
                kept.append(e)
        # 3. 输出按优先级 + 长度
        kept.sort(key=lambda x: (priority[x.entity_type], -len(x.text)))
        return kept


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def _normalize(text: str) -> str:
    return text.strip().lower()


__all__ = ["RegexEntityExtractor", "DEFAULT_STOPWORDS"]
