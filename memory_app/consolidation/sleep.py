"""SleepConsolidator —— MemScene 睡眠巩固(设计文档 §7.4)。

═══════════════════════════════════════════════════════════════════════════════
流程
═══════════════════════════════════════════════════════════════════════════════
1. ``consolidate_scene(scene)``
   - 成熟度检查:``len(member_episode_ids) >= min_members``(默认 3),否则跳过
   - 拉取所有成员 MemCell,拼接 ``text``
   - 调 LLM(prompt=``sleep_consolidation``)提炼语义陈述
   - 解析 JSON 数组 → ``SemanticMemory`` 候选
   - 每个候选过 :class:`Consolidator` 决策:
     - ADD / SUPERSEDE → 进入返回列表(由调用方持久化)
     - UPDATE → 标记为合并目标(同样进入返回列表;target_id 在 metadata)
     - NOOP → 丢弃
   - LLM 失败 → 安全返回空列表,不抛

═══════════════════════════════════════════════════════════════════════════════
失败语义
═══════════════════════════════════════════════════════════════════════════════
- 任何环节抛 → 记 warning + 返回空列表
- 单条候选解析失败 → 跳过该条;其余正常处理
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from memory_app.extractors.semantic_extractor import parse_semantic_response
from memory_app.internal_models import (
    KnowledgeType,
    MemCell,
    MemScene,
    SemanticMemory,
)
from memory_app.plugins.spi.consolidator import (
    ConsolidationDecision,
    ConsolidatorResult,
)
from memory_app.prompt_runtime import get_prompt_manager

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════════════
DEFAULT_MIN_MEMBERS = 3
DEFAULT_PROMPT_ID = "sleep_consolidation"


class SleepConsolidator:
    """睡眠巩固核心。

    构造参数:
        ``llm_client``    任意鸭子类型;``await llm.generate(prompt) -> str``
        ``mongo_repo``    实现 ``await get_by_id(mid) -> MemCell | None``
        ``consolidator``  实现 ``await consolidate(new, existing) -> ConsolidatorResult``
        ``min_members``   成熟阈值(默认 3)
        ``prompt_id``     渲染模板 ID(默认 ``sleep_consolidation``)
    """

    def __init__(
        self,
        llm_client: Any,
        mongo_repo: Any,
        consolidator: Any,
        *,
        min_members: int = DEFAULT_MIN_MEMBERS,
        prompt_id: str = DEFAULT_PROMPT_ID,
    ) -> None:
        self.llm_client = llm_client
        self.mongo_repo = mongo_repo
        self.consolidator = consolidator
        self.min_members = max(1, int(min_members))
        self.prompt_id = prompt_id

    # ────────────────────────────────────────────────────────────────────────
    # Public
    # ────────────────────────────────────────────────────────────────────────
    async def consolidate_scene(
        self,
        scene: MemScene,
        existing_facts: list[SemanticMemory] | None = None,
    ) -> list[SemanticMemory]:
        """成熟 scene → 多条 SemanticMemory(已经过 Consolidator 决策)。"""
        if len(scene.member_episode_ids) < self.min_members:
            return []
        if self.llm_client is None:
            logger.warning("sleep_consolidator: llm_client unbound, skip")
            return []

        # 1. 拉取成员 MemCell
        try:
            cells = await self._fetch_cells(scene.member_episode_ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("sleep_consolidator fetch failed: %s", e)
            return []
        if not cells:
            return []

        # 2. 渲染 prompt
        memories_text = "\n---\n".join(c.text or "" for c in cells if c.text)
        if not memories_text.strip():
            return []
        try:
            prompt = await get_prompt_manager().render_for(
                self.prompt_id,
                tenant_id=scene.tenant_id,
                user_id=scene.user_id,
                memories=memories_text,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("sleep_consolidator prompt render failed: %s", e)
            return []

        # 3. 调 LLM
        try:
            response = await self.llm_client.generate(prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("sleep_consolidator llm failed: %s", e)
            return []
        items = parse_semantic_response(response)
        if not items:
            return []

        # 4. 转 SemanticMemory + 经 Consolidator 决策
        existing = list(existing_facts or [])
        results: list[SemanticMemory] = []
        for item in items:
            mem = self._to_semantic(scene, item)
            if mem is None:
                continue
            try:
                decision = await self.consolidator.consolidate(mem, existing)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "sleep_consolidator decide failed for content=%r: %s",
                    mem.content[:32], e,
                )
                continue
            kept = self._apply_decision(mem, decision, existing)
            if kept is not None:
                results.append(kept)
                existing.append(kept)
        return results

    async def consolidate_scenes(
        self, scenes: Iterable[MemScene]
    ) -> list[SemanticMemory]:
        """批量便利 —— 多 scene 串行;失败的 scene 不影响其他。"""
        out: list[SemanticMemory] = []
        existing_per_user: dict[tuple[str, str], list[SemanticMemory]] = {}
        for sc in scenes:
            key = (sc.tenant_id, sc.user_id)
            existing = existing_per_user.setdefault(key, [])
            new_items = await self.consolidate_scene(sc, existing_facts=list(existing))
            existing.extend(new_items)
            out.extend(new_items)
        return out

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    async def _fetch_cells(self, ids: list[str]) -> list[MemCell]:
        """批量拉成员 cell —— 把 N×RTT 压成 1 次 ``$in`` 查询。

        老版 repo 缺 ``get_by_ids`` 时退化为 ``asyncio.gather``,仍并行于事件循环。
        """
        if not ids:
            return []
        batch_fn = getattr(self.mongo_repo, "get_by_ids", None)
        if callable(batch_fn):
            return await batch_fn(list(ids))
        import asyncio
        results = await asyncio.gather(
            *[self.mongo_repo.get_by_id(m) for m in ids],
            return_exceptions=False,
        )
        return [c for c in results if c is not None]

    @staticmethod
    def _to_semantic(scene: MemScene, item: dict) -> SemanticMemory | None:
        content = str(item.get("content") or "").strip()
        if not content:
            return None
        kt_raw = item.get("knowledge_type")
        try:
            kt = (
                KnowledgeType(str(kt_raw).strip().lower())
                if kt_raw
                else KnowledgeType.KNOWLEDGE
            )
        except ValueError:
            kt = KnowledgeType.KNOWLEDGE
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        return SemanticMemory(
            tenant_id=scene.tenant_id,
            user_id=scene.user_id,
            content=content,
            knowledge_type=kt,
            confidence=max(0.0, min(1.0, confidence)),
            source_episode_ids=list(scene.member_episode_ids),
        )

    @staticmethod
    def _apply_decision(
        mem: SemanticMemory,
        decision: ConsolidatorResult,
        existing: list[SemanticMemory],
    ) -> SemanticMemory | None:
        """根据决策结果返回应**真正写入**的记忆;NOOP 时返回 None。

        - ADD       → 直接返回
        - UPDATE    → 把 target_id 写入 metadata,作为合并标记;返回 mem
        - SUPERSEDE → 标记 metadata.supersedes = target_id;返回 mem
                      (调用方负责把旧 fact 的 ``is_valid`` 置 false)
        - NOOP      → None
        """
        if decision.decision == ConsolidationDecision.NOOP:
            return None
        meta_extra = {
            "consolidator_decision": decision.decision.value,
            "consolidator_sim": decision.composite_sim,
        }
        if decision.target_id and decision.decision in (
            ConsolidationDecision.UPDATE,
            ConsolidationDecision.SUPERSEDE,
        ):
            meta_extra["consolidator_target_id"] = decision.target_id
        # SemanticMemory 没有显式 metadata 字段,extra='allow' 允许写入
        for k, v in meta_extra.items():
            setattr(mem, k, v)
        return mem


__all__ = ["SleepConsolidator", "DEFAULT_MIN_MEMBERS", "DEFAULT_PROMPT_ID"]
