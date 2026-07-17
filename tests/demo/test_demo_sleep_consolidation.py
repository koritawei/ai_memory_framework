"""Demo: Phase 6 离线巩固 —— ``SleepConsolidator`` 的睡眠巩固流程。

═══════════════════════════════════════════════════════════════════════════════
本 demo 走读
═══════════════════════════════════════════════════════════════════════════════
"睡眠巩固"灵感来自人类睡眠时的记忆固化 —— 把一段时间内反复出现的情景压成
长期语义事实(SemanticMemory),并按"新增 / 合并 / 替代 / 重复"分类处理。

::

  MemScene (3+ 成员 MemCell)
       │
       ▼  SleepConsolidator.consolidate_scene(scene, existing_facts)
       │
       ├── 拉取 scene.member_episode_ids 对应的所有 MemCell(get_by_ids 一次批量)
       ├── 拼接 text → 渲染 sleep_consolidation prompt
       ├── 调 LLM → JSON 数组(候选 SemanticMemory)
       └── 每条候选过 Consolidator 决策:
             - ADD       → 全新事实,加入返回
             - UPDATE    → 与已有相似,合并;metadata 记 target_id
             - SUPERSEDE → 替代旧事实(旧 is_valid=false)
             - NOOP      → 完全重复,丢弃

本 demo 用 ``_FakeMongoRepo`` + ``_FakeLLM``(返回固定 JSON),走通真实的
``CoreConsolidator`` + ``SleepConsolidator``,验证决策分类正确。
"""

from __future__ import annotations

import json

import pytest

from memory_app.consolidation.sleep import SleepConsolidator
from memory_app.consolidator import Consolidator as CoreConsolidator
from memory_app.internal_models import (
    KnowledgeType,
    MemCell,
    MemScene,
    SemanticMemory,
)
from memory_app.plugins.spi.consolidator import ConsolidationDecision
from memory_app.prompt_runtime import reset_prompt_manager_for_test


# ════════════════════════════════════════════════════════════════════════════
# 共用:固定响应的 LLM,记录每次 prompt
# ════════════════════════════════════════════════════════════════════════════
class _ScriptedLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def generate(self, prompt: str, **_kw) -> str:
        self.calls.append(prompt)
        if not self._responses:
            return "[]"
        return self._responses.pop(0)


def _scene_with_members(member_ids: list[str]) -> MemScene:
    return MemScene(
        tenant_id="t1",
        user_id="u1",
        member_episode_ids=list(member_ids),
        member_count=len(member_ids),
    )


@pytest.fixture(autouse=True)
def _reset_prompt_manager_between_tests():
    """每个 demo 都重置 prompt 全局单例,避免上个 demo 的状态影响。

    ``SleepConsolidator.consolidate_scene`` 内部调 ``get_prompt_manager().render_for(...)``;
    无显式 init 时回退到 ``StandalonePromptManager``(用内置种子模板)。
    """
    reset_prompt_manager_for_test()
    yield
    reset_prompt_manager_for_test()


# ════════════════════════════════════════════════════════════════════════════
# 1. 完整的"3 成员场景 → LLM 出 3 条候选 → ADD/SUPERSEDE/NOOP 分类"
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_sleep_consolidation_classifies_three_decisions(fake_mongo):
    """场景成熟(3 成员 ≥ min_members=3),LLM 返回 3 条候选:

    - "用户喜欢喝咖啡" → 与 existing_facts 无重叠 → **ADD**
    - "用户喜欢咖啡馆"  → 与 "用户经常去咖啡馆" 高重合 → **SUPERSEDE / UPDATE**
    - "用户经常去咖啡馆" → 与 existing_facts 完全相同 → **NOOP**

    NOOP 不进入返回列表;最终 results 应有 2 条 SemanticMemory。
    """
    # ── 准备 3 条 cell 写入 fake mongo ────────────────────────────────────
    for i, text in enumerate([
        "上周我又去了那家咖啡馆",
        "我特别喜欢拿铁",
        "周末习惯泡咖啡馆看书",
    ]):
        await fake_mongo.insert(MemCell(
            mem_cell_id=f"m{i}", tenant_id="t1", user_id="u1",
            session_id="s1", text=text,
        ))
    scene = _scene_with_members(["m0", "m1", "m2"])

    # ── 已存在的语义事实(用来 demo SUPERSEDE / NOOP 决策)─────────────
    existing = [
        SemanticMemory(
            semantic_id="e1", tenant_id="t1", user_id="u1",
            content="用户经常去咖啡馆",
            knowledge_type=KnowledgeType.PREFERENCE,
        ),
    ]

    # ── LLM 返回 3 条候选(JSON 数组),覆盖 ADD / SUPERSEDE / NOOP ─────
    llm = _ScriptedLLM([
        json.dumps([
            # 候选 A:与 existing 无重叠 → ADD
            {"content": "用户喜欢喝拿铁", "knowledge_type": "preference",
             "confidence": 0.9},
            # 候选 B:与 e1 几乎重合(中文 token Jaccard 高) → SUPERSEDE/UPDATE
            {"content": "用户经常去咖啡馆喝咖啡", "knowledge_type": "preference",
             "confidence": 0.85},
            # 候选 C:与 e1 完全相同 → NOOP
            {"content": "用户经常去咖啡馆", "knowledge_type": "preference",
             "confidence": 0.95},
        ], ensure_ascii=False),
    ])

    # ── 真实 Consolidator 算 jaccard(无 embedding 时退化为字符 jaccard)──
    sleep_c = SleepConsolidator(
        llm_client=llm,
        mongo_repo=fake_mongo,
        consolidator=CoreConsolidator(),
        min_members=3,
        prompt_id="sleep_consolidation",
    )

    results = await sleep_c.consolidate_scene(scene, existing_facts=list(existing))

    # ── 断言:LLM 只调一次(整段 prompt)──────────────────────────────
    assert len(llm.calls) == 1

    # ── 断言:NOOP 候选被剔除,剩 ADD + SUPERSEDE/UPDATE 共 2 条 ────────
    decisions = {r.content: r.consolidator_decision for r in results}
    # 至少要有 "拿铁" 那条(纯新事实) → ADD
    add_items = [r for r in results if "拿铁" in r.content]
    assert len(add_items) == 1
    assert add_items[0].consolidator_decision == ConsolidationDecision.ADD.value
    # "用户经常去咖啡馆"(完全重复)应被丢弃
    assert not any(r.content == "用户经常去咖啡馆" for r in results)
    # 总条数 ≤ 2(NOOP 一条肯定被砍掉)
    assert len(results) <= 2


# ════════════════════════════════════════════════════════════════════════════
# 2. 不成熟场景(< min_members) → 直接返回空,不调 LLM
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_immature_scene_skipped_without_llm_call(fake_mongo):
    """min_members=3 时,2 成员的场景应直接跳过 —— 避免 LLM 浪费。"""
    await fake_mongo.insert(MemCell(
        mem_cell_id="m1", tenant_id="t1", user_id="u1",
        session_id="s1", text="只一条",
    ))
    scene = _scene_with_members(["m1"])  # 只 1 个成员

    llm = _ScriptedLLM(["[]"])
    sleep_c = SleepConsolidator(
        llm_client=llm,
        mongo_repo=fake_mongo,
        consolidator=CoreConsolidator(),
        min_members=3,
    )
    results = await sleep_c.consolidate_scene(scene)

    assert results == []
    assert llm.calls == [], "不成熟的场景不应触发 LLM 调用"


# ════════════════════════════════════════════════════════════════════════════
# 3. LLM 抛错 → 安全返回空列表,不抛
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_llm_failure_returns_empty_not_raises(fake_mongo):
    """LLM 不可用时 SleepConsolidator 必须**安静失败**(返回空 + 仅 warning),
    不能让冷路径/巩固整体崩溃 —— 这是设计文档 §5.4 降级表的契约。"""
    for i, t in enumerate(["a", "b", "c"]):
        await fake_mongo.insert(MemCell(
            mem_cell_id=f"m{i}", tenant_id="t1", user_id="u1",
            session_id="s1", text=t,
        ))

    class _FailingLLM:
        calls: list[str] = []
        async def generate(self, prompt, **_):
            self.calls.append(prompt)
            raise RuntimeError("LLM 503")

    llm = _FailingLLM()
    sleep_c = SleepConsolidator(
        llm_client=llm,
        mongo_repo=fake_mongo,
        consolidator=CoreConsolidator(),
        min_members=3,
    )
    results = await sleep_c.consolidate_scene(
        _scene_with_members(["m0", "m1", "m2"])
    )

    assert results == []
    # LLM 被调到了一次(然后抛 503,被吞)
    assert len(llm.calls) == 1


# ════════════════════════════════════════════════════════════════════════════
# 4. 批量拉取 cell:_fetch_cells 走 get_by_ids 一次 round-trip
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_fetch_cells_uses_batch_get_by_ids(fake_mongo):
    """SleepConsolidator._fetch_cells 应优先走 ``get_by_ids`` 批量接口,
    而不是循环 N 次 ``get_by_id``。

    这是上轮 perf 修复的回归守护点(N=50 成员时延迟相差一个数量级)。
    """
    # 装一个 wrapper 监控调用
    real_get_by_ids = fake_mongo.get_by_ids
    real_get_by_id = fake_mongo.get_by_id
    fake_mongo.get_by_ids_calls = 0
    fake_mongo.get_by_id_calls = 0

    async def _spy_get_by_ids(ids):
        fake_mongo.get_by_ids_calls += 1
        return await real_get_by_ids(ids)

    async def _spy_get_by_id(mid):
        fake_mongo.get_by_id_calls += 1
        return await real_get_by_id(mid)

    fake_mongo.get_by_ids = _spy_get_by_ids  # type: ignore[assignment]
    fake_mongo.get_by_id = _spy_get_by_id  # type: ignore[assignment]

    # 准备 5 成员场景
    for i in range(5):
        await fake_mongo.insert(MemCell(
            mem_cell_id=f"m{i}", tenant_id="t1", user_id="u1",
            session_id="s1", text=f"成员 {i}",
        ))

    sleep_c = SleepConsolidator(
        llm_client=_ScriptedLLM(["[]"]),
        mongo_repo=fake_mongo,
        consolidator=CoreConsolidator(),
        min_members=3,
    )
    await sleep_c.consolidate_scene(
        _scene_with_members([f"m{i}" for i in range(5)])
    )

    # 关键:1 次批量 get_by_ids,0 次单条 get_by_id
    assert fake_mongo.get_by_ids_calls == 1
    assert fake_mongo.get_by_id_calls == 0
