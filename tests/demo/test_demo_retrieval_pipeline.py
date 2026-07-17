"""Demo: Phase 4 检索五阶段管线(``POST /v1/memory/retrieve``)。

═══════════════════════════════════════════════════════════════════════════════
本 demo 走读
═══════════════════════════════════════════════════════════════════════════════
RetrievalPipeline 是整套系统里**最重**的管线 —— 一次检索请求要跨多个通道
并行召回、加权融合、信号增强、阈值过滤、最后用 MMR 兼顾相关性与多样性:

::

  RetrieveMemRequest (query + tenant + user + top_k=…)
       │
       ▼
  Stage 1: RecallStage(asyncio.gather + 单路超时)
       │     bm25_channel  ─┐
       │     vector_channel─┤  → ctx.channel_outputs: {name: [hits]}
       │     entity_channel ┤
       │     graph_channel ─┘
       │
  Stage 2: FuseStage(RRFFusion: 1/(k+rank) 加权和)
       │     去重 + 累计 score → ctx.fused: [merged hits]
       │
  Stage 3: SignalBoostStage(score × TimeDecay × (1 + Importance))
       │     → ctx.boosted
       │
  Stage 4: FilterStage(threshold filter)
       │     score < 阈值的丢弃 → ctx.filtered
       │
  Stage 5: RerankStage(MMRReranker: lam×rel - (1-lam)×max_sim_to_selected)
            截断到 top_k → ctx.final → 返回

本 demo 用**真实**的 RRFFusion / ThresholdFilter / MMRReranker(算法本身),
通道是 fake(避免依赖 ES/Milvus)。这是检验"算法装配正确"的端到端 demo。
"""

from __future__ import annotations

import pytest

from memory_app.internal_models import MemoryType, RankedMemory
from memory_app.pipelines import RetrievalPipeline
from memory_app.plugins.base import PluginError
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.retrieval.fusion import RRFConfig, RRFFusion
from memory_app.retrieval.reranker import MMRConfig, MMRReranker
from memory_app.schemas.retrieve import RetrieveMemRequest


# ════════════════════════════════════════════════════════════════════════════
# Demo 用 mock 通道 —— 不依赖 ES / Milvus
# ════════════════════════════════════════════════════════════════════════════
class _DeterministicChannel:
    """构造时给定固定命中,demo 可控分数与重叠关系。"""

    def __init__(self, name: str, hits: list[tuple[str, float]]) -> None:
        self.channel_name = name
        self._hits = hits  # [(memory_id, score), ...]
        self.calls = 0

    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]:
        self.calls += 1
        return [
            RankedMemory(
                memory_id=mid,
                memory_type=MemoryType.EPISODIC,
                content=f"text-{mid}",
                score=score,
                source_channel=self.channel_name,
                metadata={"embedding": _stable_vector(mid)},
            )
            for mid, score in self._hits
        ]


def _stable_vector(mid: str) -> list[float]:
    """从 mem_id 派生 4 维稳定向量,给 MMR 算 cosine 用 —— 不同 id 不同。"""
    # 简单可读的派生:把 mid 的字符 ord 散列到 4 维
    h = hash(mid)
    return [
        ((h >> 0) & 0xFF) / 255.0,
        ((h >> 8) & 0xFF) / 255.0,
        ((h >> 16) & 0xFF) / 255.0,
        ((h >> 24) & 0xFF) / 255.0,
    ]


# ════════════════════════════════════════════════════════════════════════════
# 1. 一条完整、健康的五阶段检索
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_retrieval_runs_all_five_stages():
    """两路通道,2 个 ID 重叠 → RRF 去重 → MMR 截到 top_k=2。

    通道布局:
    - bm25:    m1(score=0.9), m2(0.7), m3(0.5)
    - vector:  m1(0.8), m2(0.6), m4(0.4)
    并集:m1, m2, m3, m4 共 4 条;m1/m2 双通道命中 → RRF 得分更高。
    """
    bm25 = _DeterministicChannel(
        "bm25",
        [("m1", 0.9), ("m2", 0.7), ("m3", 0.5)],
    )
    vector = _DeterministicChannel(
        "vector",
        [("m1", 0.8), ("m2", 0.6), ("m4", 0.4)],
    )

    # RRF k=60(常用默认);权重让 bm25 / vector 等权,demo 简化
    fuser = RRFFusion(RRFConfig(k=60, weights={"bm25": 1.0, "vector": 1.0}))
    # MMR lambda=0.7:偏相关,留一点多样性
    reranker = MMRReranker(MMRConfig(mmr_lambda=0.7))

    pipeline = RetrievalPipeline(
        channels={"bm25": bm25, "vector": vector},
        fuser=fuser,
        filters=None,  # demo 1 不过滤,展示纯 RRF + MMR
        reranker=reranker,
        over_fetch_factor=4,
    )

    request = RetrieveMemRequest(
        tenant_id="t1", user_id="u1",
        query="北京出差",
        top_k=2,
    )

    # ── 执行 ──────────────────────────────────────────────────────────────
    final_hits = await pipeline.execute(request)

    # ── Stage 1 断言:两路通道都被调到 ──────────────────────────────────
    assert bm25.calls == 1
    assert vector.calls == 1

    # ── Stage 2 断言:RRF 去重后应有 4 条候选 ──────────────────────────
    # 验证管线 finalize 截到 top_k=2
    assert len(final_hits) == 2

    # ── m1 / m2 在两路都命中,RRF 得分应高于只命中一路的 m3 / m4 ────────
    # final 截到 top_k=2,应该是 m1 / m2 中胜出的那两个
    top_ids = {h.memory_id for h in final_hits}
    assert top_ids.issubset({"m1", "m2", "m3", "m4"})
    # m1 必中(两路都给最高分)
    assert "m1" in top_ids
    # rank 0/1 必填
    assert [h.rank for h in final_hits] == [0, 1]
    # MMR metadata 必须盖上(用于审计 / 调试)
    assert all("mmr_score" in h.metadata for h in final_hits)


# ════════════════════════════════════════════════════════════════════════════
# 2. ThresholdFilter 把低分候选剪掉
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_threshold_filter_drops_low_score_candidates():
    """RRF 默认分尺度小(单通道 1/61 ≈ 0.016;两通道 ≈ 0.033)。

    Demo 用一个介于两者之间的低阈值,演示 FilterStage 能正确切断"只单路命中"的弱候选。
    """
    bm25 = _DeterministicChannel("bm25", [("m1", 0.9), ("m2", 0.5), ("only_bm25", 0.4)])
    vector = _DeterministicChannel("vector", [("m1", 0.9), ("m2", 0.5), ("only_vec", 0.4)])

    class _CustomThreshold:
        # RRF 分数尺度小,这里写一个低阈值 filter 来演示
        async def filter(self, candidates, ctx):
            # 等权设置下,单通道命中 RRF ≈ 1/61 ≈ 0.0164;双通道命中 ≈ 2/61 ≈ 0.0328。
            # 阈值 0.025 介于两者之间,应只剩 m1 / m2,过滤掉 only_bm25 / only_vec。
            return [c for c in candidates if c.score > 0.025]

    pipeline = RetrievalPipeline(
        channels={"bm25": bm25, "vector": vector},
        # 等权配置:bm25 / vector 各 1.0,让 RRF 数学更直观可断言
        fuser=RRFFusion(RRFConfig(k=60, weights={"bm25": 1.0, "vector": 1.0})),
        filters=[_CustomThreshold()],
        reranker=MMRReranker(MMRConfig(mmr_lambda=1.0)),  # 纯相关性,便于断言
        over_fetch_factor=4,
    )
    request = RetrieveMemRequest(
        tenant_id="t1", user_id="u1", query="filter demo", top_k=10
    )
    hits = await pipeline.execute(request)
    # m1 / m2 双通道命中,RRF score ≈ 2/61 ≈ 0.033 > 0.025 → 保留
    # only_bm25 / only_vec 单通道命中,RRF score ≈ 1/63 ≈ 0.016 < 0.025 → 剪掉
    assert {h.memory_id for h in hits} == {"m1", "m2"}


# ════════════════════════════════════════════════════════════════════════════
# 3. enabled_channels 让 request 临时只走部分通道
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_enabled_channels_restricts_recall():
    """``RetrievalConfig.enabled_channels=["bm25"]`` 应只调 bm25,跳过 vector。

    这是设计文档 §2.8 五级覆盖中的 request 层 —— 客户端可临时关闭某通道。
    """
    from memory_app.schemas.retrieve import RetrievalConfig

    bm25 = _DeterministicChannel("bm25", [("m1", 0.9)])
    vector = _DeterministicChannel("vector", [("vec1", 0.9)])

    pipeline = RetrievalPipeline(
        channels={"bm25": bm25, "vector": vector},
        fuser=RRFFusion(RRFConfig(k=60, weights={"bm25": 1.0, "vector": 1.0})),
        reranker=MMRReranker(),
    )
    request = RetrieveMemRequest(
        tenant_id="t1", user_id="u1", query="single channel",
        top_k=10,
        retrieval_config=RetrievalConfig(enabled_channels=["bm25"]),
    )
    hits = await pipeline.execute(request)

    # vector 通道**没**被调到
    assert vector.calls == 0
    assert bm25.calls == 1
    # 结果只有 bm25 通道的命中
    assert {h.memory_id for h in hits} == {"m1"}


# ════════════════════════════════════════════════════════════════════════════
# 4. 全通道故障:抛 all_channels_failed (retryable=True)
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_all_channels_failed_raises_retryable_plugin_error():
    """所有通道都抛错 → RecallStage 抛 PluginError(retryable),
    让上层 HTTP 层映射为 503 + Retry-After。这是设计文档 §5.4 的关键不变量。"""

    class _BoomChannel:
        def __init__(self, name: str) -> None:
            self.channel_name = name

        async def retrieve(self, query, ctx, k):
            raise RuntimeError(f"channel {self.channel_name} down")

    pipeline = RetrievalPipeline(
        channels={"bm25": _BoomChannel("bm25"), "vector": _BoomChannel("vector")},
        fuser=RRFFusion(RRFConfig(k=60, weights={"bm25": 1.0, "vector": 1.0})),
        reranker=MMRReranker(),
    )
    request = RetrieveMemRequest(
        tenant_id="t1", user_id="u1", query="all down", top_k=5
    )

    with pytest.raises(PluginError) as ei:
        await pipeline.execute(request)
    # 上层据 retryable 决定是否抛 503 vs 500
    assert ei.value.retryable is True
    assert "all" in ei.value.code or "all" in str(ei.value)


# ════════════════════════════════════════════════════════════════════════════
# 5. 单通道故障:剩下的通道仍出结果,ctx 标记 warning
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_one_channel_fails_others_succeed():
    """vector 通道挂了,bm25 仍出结果 —— 这是检索"软降级"的核心场景。"""
    class _BoomChannel:
        channel_name = "vector"
        async def retrieve(self, query, ctx, k):
            raise RuntimeError("vector down")

    bm25 = _DeterministicChannel("bm25", [("m1", 0.9), ("m2", 0.7)])

    # 直接调底层 RecallStage 验证更直观(execute 会跑完整管线,我们想看 ctx 中间态)
    from memory_app.pipelines.retrieval import RecallStage, RetrievalPipelineContext

    recall = RecallStage(
        {"bm25": bm25, "vector": _BoomChannel()},
        timeout_per_channel_s=1.0,
    )
    ctx = RetrievalPipelineContext(
        request=RetrieveMemRequest(
            tenant_id="t1", user_id="u1", query="partial", top_k=5
        )
    )
    ctx.recall_k = 10
    ctx = await recall.run(ctx)

    # bm25 命中正常出现
    assert "bm25" in ctx.channel_outputs
    assert len(ctx.channel_outputs["bm25"]) == 2
    # vector 失败留痕但不抛
    assert "vector" in ctx.channel_warnings
    assert "vector down" in ctx.channel_warnings["vector"]
    # metrics 反映 1 ok / 1 failed
    assert ctx.metrics["channels_ok"] == 1
    assert ctx.metrics["channels_failed"] == 1
