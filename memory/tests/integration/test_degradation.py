"""故障演练:验证  降级表(管理面)。

═══════════════════════════════════════════════════════════════════════════════
覆盖的 4 类降级
═══════════════════════════════════════════════════════════════════════════════
| 演练         | 注入方式                  | 期望行为                             |
| ------------ | ------------------------- | ------------------------------------ |
| LLM 不可用   | LLM Provider build 失败   | 冷路径 skip,热路径仍可入            |
| Embedding   | Embedding build 失败      | Vector 通道无法装配,系统继续        |
| ES 不可用    | BM25 Channel retrieve 抛错 | RetrievalOrchestrator 返回 vector only |
| Milvus 不可用 | Vector Channel retrieve 抛错 | RetrievalOrchestrator 返回 BM25 only  |

═══════════════════════════════════════════════════════════════════════════════
为什么不用 docker stop
═══════════════════════════════════════════════════════════════════════════════
。
我们用 monkeypatch 模拟"组件抛 Exception",验证业务平面**正确捕获 + 降级**。
端到端的 docker 演练可在 ops/runbook 跑,不进 unit CI。

═══════════════════════════════════════════════════════════════════════════════
mark
═══════════════════════════════════════════════════════════════════════════════
打 ``@pytest.mark.integration`` —— 主仓默认 ``-m "not integration"`` 跳过。
需要时显式加 ``-m integration`` 跑;``conftest.py`` 已配置 marker。
"""

from __future__ import annotations

import pytest

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.retrieval.orchestrator import RetrievalOrchestrator
from memory_app.schemas.retrieve import RetrieveMemRequest


pytestmark = pytest.mark.integration


# ════════════════════════════════════════════════════════════════════════════
# Fakes & helpers — 鸭子类型,匹配 retrieval._ChannelProto / _FuserProto / _RerankerProto
# ════════════════════════════════════════════════════════════════════════════
class _OkChannel:
    """模拟健康通道,稳定返回若干结果。"""

    def __init__(self, name: str, hits: int = 3) -> None:
        self.channel_name = name
        self._hits = hits
        self.calls = 0

    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]:
        self.calls += 1
        return [
            RankedMemory(
                memory_id=f"id-{self.channel_name}-{i}",
                memory_type=MemoryType.EPISODIC,
                content=f"text-{self.channel_name}-{i}",
                score=1.0 - i * 0.1,
                source_channel=self.channel_name,
                metadata={},
            )
            for i in range(self._hits)
        ]


class _BoomChannel:
    """模拟故障通道,retrieve 必抛 PluginError。"""

    def __init__(self, name: str, code: str) -> None:
        self.channel_name = name
        self._code = code
        self.calls = 0

    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]:
        self.calls += 1
        raise PluginError(
            PluginErrorCategory.DEPENDENCY,
            self._code,
            f"{self.channel_name} backend is down",
            retryable=True,
        )


class _PassthroughFuser:
    """简单按 score 拼接;主要为了证明 fuser 不需要全部通道在线。"""

    async def fuse(
        self,
        channel_outputs: dict[str, list[RankedMemory]],
        weights: dict[str, float] | None = None,
    ) -> list[RankedMemory]:
        out: list[RankedMemory] = []
        for hits in channel_outputs.values():
            out.extend(hits)
        out.sort(key=lambda h: h.score, reverse=True)
        return out


class _NoopReranker:
    async def rerank(
        self,
        query: str,
        candidates: list[RankedMemory],
        top_k: int | None = None,
    ) -> list[RankedMemory]:
        if top_k is None:
            return list(candidates)
        return list(candidates)[: top_k]


def _request(top_k: int = 5) -> RetrieveMemRequest:
    return RetrieveMemRequest(
        tenant_id="t1",
        user_id="u1",
        query="今晚去哪吃",
        top_k=top_k,
    )


# ════════════════════════════════════════════════════════════════════════════
# 4 类故障 - 检索路径
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestRetrievalDegradation:
    """ES / Milvus 不可用 → orchestrator 仍返回剩余通道结果。"""

    def _build(self, channels: dict) -> RetrievalOrchestrator:
        return RetrievalOrchestrator(
            channels=channels,
            fuser=_PassthroughFuser(),
            reranker=_NoopReranker(),
            filters=[],
        )

    async def test_es_down_falls_back_to_vector(self):
        bm25 = _BoomChannel("bm25", code="es_unavailable")
        vector = _OkChannel("vector", hits=3)
        orch = self._build({"bm25": bm25, "vector": vector})

        result = await orch.execute(_request(top_k=5))
        # ES 抛错被通道隔离,vector 仍贡献结果
        assert any(h.source_channel == "vector" for h in result)
        assert all(h.source_channel != "bm25" for h in result)
        # 不应整体 503
        assert len(result) == 3
        # 故障通道确实被调用过
        assert bm25.calls == 1
        assert vector.calls == 1

    async def test_milvus_down_falls_back_to_bm25(self):
        bm25 = _OkChannel("bm25", hits=4)
        vector = _BoomChannel("vector", code="milvus_unavailable")
        orch = self._build({"bm25": bm25, "vector": vector})

        result = await orch.execute(_request(top_k=5))
        assert any(h.source_channel == "bm25" for h in result)
        assert all(h.source_channel != "vector" for h in result)
        assert len(result) == 4
        assert bm25.calls == 1
        assert vector.calls == 1

    async def test_both_down_raises_plugin_error(self):
        # 设计契约:全失败 → PluginError(all_channels_failed, retryable=True)
        # 路由层据此返回 503;不应静默返回空(否则与"真无结果"无法区分)
        orch = self._build(
            {
                "bm25": _BoomChannel("bm25", code="es_unavailable"),
                "vector": _BoomChannel("vector", code="milvus_unavailable"),
            }
        )
        with pytest.raises(PluginError) as ei:
            await orch.execute(_request(top_k=5))
        assert ei.value.code == "all_channels_failed"
        assert ei.value.retryable is True


# ════════════════════════════════════════════════════════════════════════════
# 冷路径降级:LLM / Embedding 不可用
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestColdPathDegradation:
    """LLM Provider 不可用 → 整个冷路径 skip,热路径不受影响。"""

    async def test_llm_unavailable_skips_cold_path(self):
        from memory_app.deps import AppState
        from memory_app.deps.builders.cold_path import ColdPathServiceBuilder

        state = AppState.__new__(AppState)
        state.cold_path_service = None
        state.background_runner = None
        state.dlq = None
        state.ingest_service = None
        state.entity_extractor = None

        class _Fact:
            async def build(self, category, *a, **kw):
                if "llm" in category:
                    raise LookupError(f"{category} not configured")
                raise LookupError(f"{category} not used in this test")

        state.plugin_factory = _Fact()
        # 不抛,直接 return
        await ColdPathServiceBuilder().build(state)
        assert state.cold_path_service is None

    async def test_embedding_unavailable_does_not_break_retrieval_init(self):
        from memory_app.deps import AppState
        from memory_app.deps.builders.retrieval import RetrievalOrchestratorBuilder
        from memory_app.deps.clients import ExternalClients

        state = AppState.__new__(AppState)
        state.retrieval_orchestrator = None
        state.lifecycle_updater = None
        state.background_runner = None
        state.settings = None
        # RetrievalOrchestratorBuilder 通过 state.clients.* 读 ES / Milvus 配置
        state.clients = ExternalClients()

        class _Fact:
            async def build(self, category, *a, **kw):
                # 任何 build 都失败,模拟无 embedding / 无任何通道
                raise LookupError(f"{category} not configured")

        state.plugin_factory = _Fact()
        # 在所有可降级组件不可达时,builder 应仅 warn 不抛
        await RetrievalOrchestratorBuilder().build(state)
        # orchestrator 可能保持 None(无任何通道),但绝不 throw 上传
        assert state.retrieval_orchestrator is None or hasattr(
            state.retrieval_orchestrator, "execute"
        )


# ════════════════════════════════════════════════════════════════════════════
# 单通道超时 — 不应让其他通道陪绑
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestPerChannelTimeout:
    async def test_one_slow_channel_does_not_block_others(self):
        import asyncio

        class _SlowChannel:
            channel_name = "slow"

            async def retrieve(self, query, ctx, k):
                await asyncio.sleep(10.0)  # 远超 timeout
                return []

        slow = _SlowChannel()
        fast = _OkChannel("fast", hits=2)
        orch = RetrievalOrchestrator(
            channels={"slow": slow, "fast": fast},
            fuser=_PassthroughFuser(),
            reranker=_NoopReranker(),
            filters=[],
            timeout_per_channel_s=0.1,  # 拉短到 100ms 加速测试
        )
        result = await orch.execute(_request(top_k=5))
        # fast 通道结果照常返回;slow 因超时被丢
        assert any(h.source_channel == "fast" for h in result)
        assert all(h.source_channel != "slow" for h in result)
