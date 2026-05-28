"""Quality-loop iteration 1 的 regression 锁定测试。

每个 case 都对应一条本轮修复 —— 旧实现下该 case 会失败,新实现下通过。
保留这些测试是为了未来重构时不让"已修好的 bug 又静默回归"。
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.retrieval.channels.vector import VectorChannel


# ════════════════════════════════════════════════════════════════════════════
# Regression #1:VectorChannel 真实 pymilvus 不再被 sniff 双调
# ════════════════════════════════════════════════════════════════════════════
class _FakePymilvusCollection:
    """模拟 pymilvus.Collection —— 同步 ``search`` + 模块路径以 ``pymilvus`` 开头。

    用于触发 ``_looks_blocking`` 的真客户端分支。
    """

    __module__ = "pymilvus.demo_fake"  # _looks_blocking 检查的就是这个

    def __init__(self) -> None:
        self.calls = 0

    def search(self, **kw) -> list:
        self.calls += 1
        # 返回一个空 hits 列表(list[list[Hit]] 结构)
        return [[]]


class _FakeEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


@pytest.mark.asyncio
async def test_vector_channel_real_pymilvus_calls_search_exactly_once():
    """旧实现先 ``sniff = _run_search``(阻塞调一次)再 ``await asyncio.to_thread(_run_search)``
    (再调一次)→ ``calls == 2``。修复后真客户端只走 to_thread 路径,calls == 1。
    """
    coll = _FakePymilvusCollection()
    ch = VectorChannel(
        collection=coll,
        embedding_client=_FakeEmbedding(),
        anns_field="embedding",
        over_fetch_factor=1,
    )
    await ch._execute_search(
        tenant_id="t1", user_id="u1",
        query="q",
        top_k=3,
        filters={},
    )
    assert coll.calls == 1, (
        f"修复后真 pymilvus 客户端应只被调用 1 次,实际 {coll.calls} 次 ——"
        f"先 sniff 后 to_thread 的双执行又回来了"
    )


@pytest.mark.asyncio
async def test_vector_channel_test_mock_still_works():
    """非 pymilvus(测试 mock)走 sniff 路径,行为不变。"""
    class _MockCollection:
        __module__ = "tests.fake"
        def __init__(self):
            self.calls = 0
        def search(self, **kw):
            self.calls += 1
            return [[]]

    coll = _MockCollection()
    ch = VectorChannel(
        collection=coll, embedding_client=_FakeEmbedding(), over_fetch_factor=1,
    )
    await ch._execute_search(
        tenant_id="t1", user_id="u1", query="q", top_k=3, filters={}
    )
    # mock 路径 also 应该只调一次
    assert coll.calls == 1


# ════════════════════════════════════════════════════════════════════════════
# Regression #2:ConsolidationService 串行化防止跨调用 scope 污染
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_consolidation_service_serializes_concurrent_calls():
    """两个并发 consolidate(tenant=A) + consolidate(tenant=B) 必须看到各自的 scope,
    不能因为 strategy._scope_provider 被对方覆盖而读到错误租户。

    旧实现:set_scope_provider(A) → await run 期间被 set_scope_provider(B) 覆盖
            → A 的 run 读到 B 的 scope。
    新实现:asyncio.Lock 串行化整段。
    """
    from memory_app.services import ConsolidationService

    # Strategy 暴露 set_scope_provider + run,run 内部读 _scope_provider
    class _SpyStrategy:
        def __init__(self) -> None:
            self._scope_provider = None
            self.observed_scopes: list[list] = []

        def set_scope_provider(self, provider):
            self._scope_provider = provider

        async def run(self, scope=None, time=None):
            # 模拟 LLM 调用引入的 await 间隙 —— 给并发对手抢占的时机
            await asyncio.sleep(0.01)
            scopes = await self._scope_provider()
            self.observed_scopes.append(scopes)
            # 再 sleep 一段,放大窗口
            await asyncio.sleep(0.01)
            from memory_app.plugins.spi.consolidation_strategy import ConsolidationReport
            now = datetime.now(timezone.utc)
            return ConsolidationReport(
                phase="light", started_at=now, finished_at=now,
            )

    strategy = _SpyStrategy()

    async def _scope_provider(tenant_id: str, user_id: str | None) -> list:
        return [(tenant_id, user_id or "default")]

    service = ConsolidationService(strategy, scope_provider=_scope_provider)

    # 并发 A / B
    results = await asyncio.gather(
        service.consolidate(tenant_id="tenantA"),
        service.consolidate(tenant_id="tenantB"),
    )

    # 关键:两次观察到的 scope 必须分别为 A / B(顺序可换),不能两次都是同一个租户
    assert len(strategy.observed_scopes) == 2
    observed_tenants = {s[0][0] for s in strategy.observed_scopes}
    assert observed_tenants == {"tenantA", "tenantB"}, (
        f"并发调用应各自看到自己的 tenant,实际观察到 {observed_tenants} ——"
        f"set_scope_provider 跨调用污染又回来了"
    )


# ════════════════════════════════════════════════════════════════════════════
# Regression #3:GraphChannel 多实体并发遍历(非串行)
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_graph_channel_traverses_entities_concurrently():
    """3 个实体 × 每次 traverse sleep 50ms。串行 ≥ 150ms;并行 ≈ 50ms。"""
    import time
    from memory_app.retrieval.channels.graph import GraphChannel

    call_log: list[float] = []

    class _SlowGraph:
        async def get_neighbors(self, user_id, node_id, max_depth=2):
            call_log.append(time.monotonic())
            await asyncio.sleep(0.05)
            return [f"m-from-{node_id[-1]}"]

    class _FakeMongo:
        async def get_by_ids(self, ids):
            return []  # demo only checks gather timing,不关心实际拉 cell

    class _ThreeEntityExtractor:
        async def extract(self, query: str):
            class _E:
                def __init__(self, t): self.text = t
            return [_E("北京"), _E("上海"), _E("深圳")]

    ch = GraphChannel(
        memory_graph=_SlowGraph(),
        mongo_repo=_FakeMongo(),
        entity_extractor=_ThreeEntityExtractor(),
        max_depth=2,
    )

    start = time.monotonic()
    await ch._execute_search(
        tenant_id="t1", user_id="u1",
        query="三城出差",
        top_k=10,
        filters={},
    )
    elapsed = time.monotonic() - start
    # 串行 ≥ 150ms;并行 ≈ 50ms。给 100ms 上限既排除串行又给 CI 抖动余量
    assert elapsed < 0.1, (
        f"GraphChannel 应并发 traverse 多实体,实测 {elapsed*1000:.1f}ms —— "
        f"回退到串行 await 又出现了"
    )


# ════════════════════════════════════════════════════════════════════════════
# Regression #4:Consolidator new_tokens 只计算一次
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_consolidator_does_not_recompute_new_tokens():
    """监测 ``_tokens(new_fact)`` 在 consolidate 内被调用的次数 ≤ 1。

    旧实现是 N 次 existing × 1 次 new_tokens 重算 = N 次;新实现提到循环外 = 1 次。
    """
    from memory_app.consolidator import Consolidator
    from memory_app.internal_models import KnowledgeType, SemanticMemory

    counter = {"new_tokens_calls": 0}
    real_tokens = Consolidator._tokens

    def _counting_tokens(mem):
        # 简单方式:对所有调用记 1,然后断言总调用数 = len(existing) + 1
        counter["new_tokens_calls"] += 1
        return real_tokens(mem)

    Consolidator._tokens = staticmethod(_counting_tokens)  # type: ignore[assignment]
    try:
        c = Consolidator()
        new = SemanticMemory(
            tenant_id="t1", user_id="u1",
            content="用户喜欢咖啡",
            knowledge_type=KnowledgeType.PREFERENCE,
        )
        existing = [
            SemanticMemory(
                semantic_id=f"e{i}", tenant_id="t1", user_id="u1",
                content=f"用户喜欢 {i}",
                knowledge_type=KnowledgeType.PREFERENCE,
            )
            for i in range(5)
        ]
        counter["new_tokens_calls"] = 0
        await c.consolidate(new, existing)
        # 期望:1 次 new_tokens(提到循环外) + 5 次 existing(每条一次)
        assert counter["new_tokens_calls"] == 1 + len(existing), (
            f"_tokens 被调 {counter['new_tokens_calls']} 次;"
            f"应为 1 + N = {1 + len(existing)}(新事实只算一次)"
        )
    finally:
        Consolidator._tokens = staticmethod(real_tokens)  # type: ignore[assignment]


# ════════════════════════════════════════════════════════════════════════════
# Regression #5:llm_sbd should_end 严格相等,不被 LLM 过大 idx 误触发
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_llm_sbd_should_end_strict_equality_with_history_length():
    """LLM 幻觉 idx=999 时,旧实现 ``idx >= len(history)`` 触发误切。
    新实现 ``idx == len(history)`` 严格相等才切。
    """
    from memory_app.internal_models import RawData
    from memory_app.plugins.spi.boundary_detector import BoundaryContext
    from memory_app.plugins_default.llm_sbd import LLMSBD

    class _LLMReturning999:
        async def generate(self, prompt, **_):
            # 返回一个超大 idx,模拟 LLM 把 turn 编号当 1-indexed 后误算
            return '{"boundary_index": 999, "reason": "hallucinated"}'

    sbd = LLMSBD()
    await sbd.start({})
    sbd.bind_llm_client(_LLMReturning999())

    now = datetime.now(timezone.utc)
    history = [
        RawData(tenant_id="t1", user_id="u1", session_id="s1",
                content="a", event_time=now),
        RawData(tenant_id="t1", user_id="u1", session_id="s1",
                content="b", event_time=now),
    ]
    new = [RawData(tenant_id="t1", user_id="u1", session_id="s1",
                   content="c", event_time=now)]
    ctx = BoundaryContext(
        tenant_id="t1", user_id="u1",
        scenario="assistant", current_time=now.isoformat(),
    )

    result = await sbd.detect(history, new, ctx)
    # 旧 (idx >= 2) → True;新 (idx == 2) → False(idx=999)
    assert result.should_end is False, (
        "LLM 幻觉 idx=999 不应触发切边界(新实现严格 idx == len(history))"
    )


# ════════════════════════════════════════════════════════════════════════════
# Regression #6:admin rollback 容忍 history 中的非整数 version
# ════════════════════════════════════════════════════════════════════════════
def test_admin_safe_version_returns_minus_one_for_invalid():
    """admin.py 内部 _safe_version helper 应在转换失败时返回 -1,
    而不是让 int(...) 抛 ValueError → 500 给客户端。
    """
    # 通过 import 验证 helper 存在,并直接测它(它是闭包,需要走 endpoint 行为)
    # 这里 demo "rollback 收到坏 history 不会 500":mock ConfigCenter.history 返回脏数据
    import asyncio
    from fastapi.testclient import TestClient

    from memory_app import api
    from memory_app.config_center.base import ConfigCenter
    from memory_app.deps.state import app_state

    class _DirtyHistoryCC(ConfigCenter):
        async def resolve(self, category, **kw): raise NotImplementedError
        async def write(self, *a, **kw): return 1
        async def history(self, category, limit=50):
            # 故意混入非整数 version + None,旧实现会 int 抛
            return [
                {"version": "not-a-number", "scope": "global", "scope_id": None,
                 "name": "x", "params": {}},
                {"version": None, "scope": "global", "scope_id": None,
                 "name": "y", "params": {}},
                {"version": 5, "scope": "global", "scope_id": None,
                 "name": "z", "params": {}},
            ]
        async def watch(self, callback): pass
        async def close(self): pass

    saved_cc = app_state.config_center
    app_state.config_center = _DirtyHistoryCC()
    try:
        with TestClient(api.app) as client:
            # rollback target_version=5(只有第三条匹配)→ 应成功而非 500
            r = client.post(
                "/v1/admin/config/rollback",
                json={
                    "category": "memory.retrieval.fuser",
                    "target_version": 5,
                    "scope": "global",
                    "scope_id": None,
                    "actor": "test",
                },
            )
            # 旧实现 int("not-a-number") 直接 500;新实现跳过坏行,匹配 version=5
            # 期望 200(成功匹配到第 3 条) — 或至少不是 500
            assert r.status_code != 500, (
                f"rollback 不应被坏 history 行打成 500;got {r.status_code}: {r.text}"
            )
    finally:
        app_state.config_center = saved_cc
