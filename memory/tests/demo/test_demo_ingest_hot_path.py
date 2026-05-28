"""Demo: 写入热路径 写入热路径(``POST /v1/memory/ingest``)。

═══════════════════════════════════════════════════════════════════════════════
本 demo 走读
═══════════════════════════════════════════════════════════════════════════════
按"一条写入请求"在系统里的真实顺序展开:

::

  HTTP /v1/memory/ingest
       │
       │  router 层(routers/memory.py)
       │  调 format_transfer 把外部 MemoryIngestRequest 转 list[RawData]
       │
       ▼
  IngestPipeline.execute(raw_data_list)
       │
       ├── Stage 1: SegmentStage (rule_sbd)
       │       按时间窗 / token 数把 RawData 切成多段 segments
       │
       ├── Stage 2: PersistMemCellStage
       │       每段包装为 MemCell + 一次 insert_many 写入 MongoDB(SOT)
       │
       └── Stage 3: SyncIndexStage
               ES 索引 + Milvus 向量;失败仅入 DLQ,不阻塞热路径返回
       │
       ▼
  返回 list[mem_cell_id] —— HTTP 200

本 demo 不调任何真实 Mongo / ES / Milvus —— 三库均由 ``conftest.py`` 的内存 fake
模拟,仅断言每个阶段对 fake 的副作用。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory_app.internal_models import RawData
from memory_app.pipelines import IngestPipeline
from memory_app.repositories.dlq import InMemoryDLQ
from memory_app.repositories.es_repo import ESMemCellRepo
from memory_app.repositories.milvus_repo import MilvusMemCellRepo


# ════════════════════════════════════════════════════════════════════════════
# Demo 用 SBD —— 不挂 LLM,按 session_id 平凡分段
# ════════════════════════════════════════════════════════════════════════════
class _SessionBoundarySegmenter:
    """同一个 session_id 的 RawData 归并成一段;不同 session 各自一段。

    这模拟最简单的 "rule_sbd" 行为,避免 demo 引入 SBD 算法细节 ——
    真正的 SBD 测试在 ``test_sbd.py``。
    """

    async def segment(self, raws: list[RawData]) -> list[list[RawData]]:
        by_session: dict[str, list[RawData]] = {}
        for r in raws:
            by_session.setdefault(r.session_id, []).append(r)
        return list(by_session.values())


# ════════════════════════════════════════════════════════════════════════════
# 1. 走一条完整、健康的写入路径
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_full_ingest_writes_to_mongo_es_and_skips_milvus_without_embedding(
    fake_mongo, fake_es, fake_milvus
):
    """两个 session,各两条 turn → 切成两段 segment → 两个 MemCell 落 Mongo + ES。

    关键不变量:**没有 embedding 的 MemCell 跳过 Milvus 写**(冷路径补 embedding
    后由 reconciler 再补 Milvus,不算热路径失败)。
    """
    # ── 构造输入:两个 session,每个 session 一条 RawData ────────────────
    now = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
    raws = [
        RawData(
            tenant_id="t1", user_id="u1", session_id="s1",
            content="我下周要去北京出差",
            event_time=now,
        ),
        RawData(
            tenant_id="t1", user_id="u1", session_id="s2",
            content="顺便提一下,周末喜欢去咖啡馆",
            event_time=now,
        ),
    ]

    # ── 装配 IngestPipeline(注入 fake 仓储)──────────────────────────────
    # MilvusMemCellRepo 接受 ``insert_callable``,让我们把 fake 注入而不必启 Milvus 客户端
    milvus_repo = MilvusMemCellRepo(insert_callable=fake_milvus)
    es_repo = ESMemCellRepo(fake_es, index_prefix="demo")
    dlq = InMemoryDLQ()

    pipeline = IngestPipeline(
        segmenter=_SessionBoundarySegmenter(),
        mem_cell_repo=fake_mongo,
        es_repo=es_repo,
        milvus_repo=milvus_repo,   # 无 embedding 不会真的写
        dlq=dlq,
    )

    # ── 执行:管线一气呵成跑完三个 stage ─────────────────────────────────
    mem_cell_ids = await pipeline.execute(raws)

    # ── 断言:返回值 = 两个 MemCell 的 id,顺序与 segment 顺序一致 ──────
    assert len(mem_cell_ids) == 2, "两 session → 两 MemCell"
    assert all(isinstance(mid, str) and mid for mid in mem_cell_ids),\
        "mem_cell_id 必须是非空字符串"

    # ── 断言:SOT(MongoDB)写入正确 ──────────────────────────────────────
    assert len(fake_mongo.store) == 2
    cell1 = fake_mongo.store[mem_cell_ids[0]]
    cell2 = fake_mongo.store[mem_cell_ids[1]]
    # tenant / user 必须跟随入参传透
    assert cell1.tenant_id == "t1" and cell1.user_id == "u1"
    # text = 拼接 segment 内所有 RawData.content
    texts = {cell1.text, cell2.text}
    assert "我下周要去北京出差" in texts
    assert "顺便提一下,周末喜欢去咖啡馆" in texts

    # ── 断言:ES 索引也写了两条(与 Mongo 数量对齐)────────────────────
    assert len(fake_es.indexed) == 2
    indexed_ids = {doc_id for _, doc_id, _ in fake_es.indexed}
    assert indexed_ids == set(mem_cell_ids)
    # ES 文档必须含 BM25 主字段
    _, _, sample_doc = fake_es.indexed[0]
    assert "text" in sample_doc
    assert sample_doc["tenant_id"] == "t1"

    # ── 断言:Milvus 无调用 —— 因为 cell 没有 embedding(关键不变量)────
    assert fake_milvus.calls == [], (
        "写入热路径 默认 cell 无 embedding;Milvus 应被跳过,不报错也不入 DLQ"
    )

    # ── 断言:DLQ 为空(全链路成功)──────────────────────────────────────
    dlq_records = await dlq.list()
    assert dlq_records == []


# ════════════════════════════════════════════════════════════════════════════
# 2. 带 embedding 的写入路径会真的调到 Milvus
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_ingest_with_embedding_writes_to_milvus(
    fake_mongo, fake_es, fake_milvus
):
    """如果上游(冷路径回填 / 客户端直传)已带 embedding,Milvus 应被调用。"""
    raws = [
        RawData(
            tenant_id="t1", user_id="u1", session_id="s1",
            content="带 embedding 的输入",
            event_time=datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
            # RawData 自身没有 embedding 字段;embedding 在 MemCell 上,
            # 这里通过自定义 segmenter 给出带 embedding 的 cell。
        ),
    ]

    # 自定义 segmenter:直接构造带 embedding 的 MemCell
    from memory_app.internal_models import MemCell

    class _EmbeddedSegmenter:
        async def segment(self, raws):
            # 简化:直接返回原 raw,后续 PersistMemCellStage 包装为 MemCell
            return [list(raws)]

    # 注入带 embedding 的 cell:我们用 monkeypatch 让 PersistMemCellStage._build_cell
    # 返回带 embedding 的版本,而不是改基础设施代码。这里走更简洁的路径 ——
    # 直接调 ingest_pipeline,再事后给 cell 补 embedding 重跑同步(模拟"冷路径回填后
    # 调用 SyncIndexStage.run 重试")
    from memory_app.pipelines import IngestPipelineContext, SyncIndexStage

    cell = MemCell(
        tenant_id="t1", user_id="u1", session_id="s1",
        raw_data_ids=[raws[0].raw_id],
        text="带 embedding 的输入",
        embedding=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],  # 8 维 mock
    )
    await fake_mongo.insert(cell)

    sync_stage = SyncIndexStage(
        es_repo=ESMemCellRepo(fake_es, index_prefix="demo"),
        milvus_repo=MilvusMemCellRepo(insert_callable=fake_milvus),
        dlq=InMemoryDLQ(),
    )
    ctx = IngestPipelineContext(raw_data_list=raws)
    ctx.cells.append(cell)
    await sync_stage.run(ctx)

    # 断言:Milvus 这次真被调了
    assert len(fake_milvus.calls) == 1
    mid, vec, meta = fake_milvus.calls[0]
    assert mid == cell.mem_cell_id
    assert len(vec) == 8
    assert meta["tenant_id"] == "t1" and meta["user_id"] == "u1"


# ════════════════════════════════════════════════════════════════════════════
# 3. 故障演练:ES 抛错时,DLQ 记录失败,**热路径仍返回成功**
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_ingest_es_failure_records_dlq_but_succeeds(
    fake_mongo, fake_milvus
):
    """这是 降级表的关键不变量 —— ES 抖动**不**阻塞业务平面写入。

    诊断价值:
    - 热路径返回的 mem_cell_id 数量与请求一致
    - SOT(Mongo)写入成功(后续 reconciler 可从 Mongo 重建 ES)
    - DLQ 收到与请求等量的 ES 失败记录
    """
    failing_es = type(  # 一次性 fake:不再依赖 conftest 的 fake_es,避免 fixture 被改坏
        "_FailingES",
        (),
        {
            "indexed": [],
            "indices": type("_I", (), {
                "exists": staticmethod(lambda **_: False),
                "create": staticmethod(lambda **_: None),
            })(),
            "index": lambda self, **kw: (_ for _ in ()).throw(RuntimeError("ES down")),
            "delete": lambda self, **kw: None,
        },
    )()

    es_repo = ESMemCellRepo(failing_es, index_prefix="demo")
    dlq = InMemoryDLQ()

    pipeline = IngestPipeline(
        segmenter=_SessionBoundarySegmenter(),
        mem_cell_repo=fake_mongo,
        es_repo=es_repo,
        milvus_repo=MilvusMemCellRepo(insert_callable=fake_milvus),
        dlq=dlq,
    )
    raws = [
        RawData(
            tenant_id="t1", user_id="u1", session_id="s1",
            content="ES down 演练",
            event_time=datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
        ),
    ]

    # 关键:execute **不**抛异常 —— ES 失败仅记 DLQ
    mem_cell_ids = await pipeline.execute(raws)
    assert len(mem_cell_ids) == 1, "热路径不被 ES 故障阻塞"

    # SOT 写入成功
    assert len(fake_mongo.store) == 1

    # DLQ 留下了恰好 1 条 ES 失败记录
    records = await dlq.list()
    es_records = [r for r in records if r.target == "es"]
    assert len(es_records) == 1
    assert es_records[0].mem_cell_id == mem_cell_ids[0]
    assert "ES down" in es_records[0].error


# ════════════════════════════════════════════════════════════════════════════
# 4. 空输入的优雅处理
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_demo_empty_ingest_returns_empty_list(
    fake_mongo, fake_es, fake_milvus
):
    """空输入应该返回空列表,不触发任何写入(防御性 demo)。"""
    pipeline = IngestPipeline(
        segmenter=_SessionBoundarySegmenter(),
        mem_cell_repo=fake_mongo,
        es_repo=ESMemCellRepo(fake_es, index_prefix="demo"),
        milvus_repo=MilvusMemCellRepo(insert_callable=fake_milvus),
        dlq=InMemoryDLQ(),
    )
    result = await pipeline.execute([])
    assert result == []
    assert fake_mongo.store == {}
    assert fake_es.indexed == []
    assert fake_milvus.calls == []
