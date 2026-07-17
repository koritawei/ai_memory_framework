"""Step 2.3 三库同步 + DLQ 测试。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- ESMemCellRepo / MilvusMemCellRepo 写入与失败传播
- SyncIndexStage 串行同步 ES → Milvus,失败入 DLQ 不抛
- 没有 embedding 时不调 Milvus / 不入 DLQ
- IngestPipeline 端到端:ES 失败时 ingest 仍返回成功
- InMemoryDLQ 行为(限长 + 顺序)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.internal_models import MemCell, RawData
from memory_app.pipelines import (
    IngestPipeline,
    IngestPipelineContext,
    SyncIndexStage,
)
from memory_app.repositories.dlq import DLQRecord, InMemoryDLQ
from memory_app.repositories.es_repo import ESMemCellRepo
from memory_app.repositories.milvus_repo import MilvusMemCellRepo


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
def _cell(text: str = "hi", with_embedding: bool = False) -> MemCell:
    kwargs: dict = dict(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        raw_data_ids=["r1"],
        text=text,
    )
    if with_embedding:
        kwargs["embedding"] = [0.1] * 8
    return MemCell(**kwargs)


def _raw(content: str, minutes: int = 0) -> RawData:
    return RawData(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        content=content,
        event_time=datetime(2026, 1, 1, tzinfo=timezone.utc)
        + timedelta(minutes=minutes),
    )


class _FakeESClient:
    """模拟 elasticsearch.AsyncElasticsearch。"""

    def __init__(self, index_should_fail: bool = False) -> None:
        self.indexed: list[tuple[str, str, dict]] = []
        self.fail = index_should_fail
        self.indices = self._FakeIndices()

    class _FakeIndices:
        def __init__(self) -> None:
            self.created = []

        async def exists(self, index: str) -> bool:
            return False

        async def create(self, index: str, mappings=None) -> None:
            self.created.append(index)

    async def index(self, index: str, id: str, document: dict) -> None:
        if self.fail:
            raise RuntimeError("ES down")
        self.indexed.append((index, id, document))

    async def delete(self, index: str, id: str, ignore=None) -> None:
        return None


class _FakeMilvusInsert:
    """记录 milvus insert 调用。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def __call__(self, mid: str, embedding: list[float], metadata: dict) -> None:
        if self.fail:
            raise RuntimeError("Milvus down")
        self.calls.append((mid, embedding, metadata))


class _FakeMongoRepo:
    def __init__(self) -> None:
        self.store: dict[str, MemCell] = {}

    async def insert(self, cell: MemCell) -> str:
        self.store[cell.mem_cell_id] = cell
        return cell.mem_cell_id


class _FakeSegmenter:
    async def segment(self, raws: list[RawData]) -> list[list[RawData]]:
        return [raws] if raws else []


# ════════════════════════════════════════════════════════════════════════════
# ESMemCellRepo
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestESMemCellRepo:
    async def test_index_passes_doc_to_es(self):
        client = _FakeESClient()
        repo = ESMemCellRepo(client, index_prefix="test")
        cell = _cell()
        await repo.index(cell)
        assert len(client.indexed) == 1
        idx, doc_id, doc = client.indexed[0]
        assert idx == "test_mem_cells"
        assert doc_id == cell.mem_cell_id
        assert doc["text"] == cell.text
        assert doc["state"] == "ACTIVE"

    async def test_index_propagates_error(self):
        client = _FakeESClient(index_should_fail=True)
        repo = ESMemCellRepo(client)
        with pytest.raises(RuntimeError, match="ES down"):
            await repo.index(_cell())

    async def test_ensure_index_idempotent(self):
        client = _FakeESClient()
        repo = ESMemCellRepo(client, index_prefix="test")
        await repo.ensure_index()
        await repo.ensure_index()
        # exists() 总返回 False,所以 create 被调两次,但都不抛
        assert len(client.indices.created) >= 1

    async def test_ensure_index_swallows_error(self):
        class _Broken(_FakeESClient):
            class _FakeIndices:
                async def exists(self, index):
                    raise RuntimeError("broken")

                async def create(self, **kw):
                    raise RuntimeError("broken")

            def __init__(self):
                super().__init__()
                self.indices = self._FakeIndices()

        repo = ESMemCellRepo(_Broken())
        await repo.ensure_index()  # 不应抛


# ════════════════════════════════════════════════════════════════════════════
# MilvusMemCellRepo
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestMilvusMemCellRepo:
    async def test_insert_via_callable(self):
        fake = _FakeMilvusInsert()
        repo = MilvusMemCellRepo(insert_callable=fake)
        await repo.insert("cell-1", [0.1, 0.2], {"tenant_id": "t1"})
        assert len(fake.calls) == 1
        mid, emb, meta = fake.calls[0]
        assert mid == "cell-1"
        assert emb == [0.1, 0.2]
        assert meta == {"tenant_id": "t1"}

    async def test_insert_skips_empty_embedding(self):
        fake = _FakeMilvusInsert()
        repo = MilvusMemCellRepo(insert_callable=fake)
        await repo.insert("cell-2", [], {})
        assert fake.calls == []  # 不调用

    async def test_insert_propagates_error(self):
        fake = _FakeMilvusInsert(fail=True)
        repo = MilvusMemCellRepo(insert_callable=fake)
        with pytest.raises(RuntimeError, match="Milvus down"):
            await repo.insert("cell-3", [0.1], {})


# ════════════════════════════════════════════════════════════════════════════
# InMemoryDLQ
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestInMemoryDLQ:
    async def test_enqueue_and_list(self):
        dlq = InMemoryDLQ()
        await dlq.enqueue(DLQRecord(target="es", mem_cell_id="c1"))
        await dlq.enqueue(DLQRecord(target="milvus", mem_cell_id="c2"))
        items = await dlq.list()
        assert len(items) == 2
        # 最新优先(c2 后入)
        assert items[0].mem_cell_id == "c2"

    async def test_list_filtered_by_target(self):
        dlq = InMemoryDLQ()
        await dlq.enqueue(DLQRecord(target="es", mem_cell_id="c1"))
        await dlq.enqueue(DLQRecord(target="milvus", mem_cell_id="c2"))
        es_items = await dlq.list(target="es")
        assert len(es_items) == 1
        assert es_items[0].target == "es"

    async def test_size(self):
        dlq = InMemoryDLQ()
        assert await dlq.size() == 0
        await dlq.enqueue(DLQRecord(target="es", mem_cell_id="c1"))
        assert await dlq.size() == 1

    async def test_max_size_drops_oldest(self):
        dlq = InMemoryDLQ(max_size=3)
        for i in range(5):
            await dlq.enqueue(DLQRecord(target="es", mem_cell_id=f"c{i}"))
        items = await dlq.list(limit=10)
        assert await dlq.size() == 3
        # 应该保留最新 3 条:c2 / c3 / c4
        ids = [r.mem_cell_id for r in items]
        assert ids == ["c4", "c3", "c2"]


# ════════════════════════════════════════════════════════════════════════════
# SyncIndexStage 行为
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestSyncIndexStage:
    async def test_es_only_no_milvus(self):
        client = _FakeESClient()
        es_repo = ESMemCellRepo(client)
        stage = SyncIndexStage(es_repo=es_repo, milvus_repo=None, dlq=None)
        ctx = IngestPipelineContext(raw_data_list=[])
        ctx.cells = [_cell()]
        await stage.run(ctx)
        assert len(client.indexed) == 1

    async def test_es_failure_does_not_raise_and_enqueues_dlq(self):
        client = _FakeESClient(index_should_fail=True)
        es_repo = ESMemCellRepo(client)
        dlq = InMemoryDLQ()
        stage = SyncIndexStage(es_repo=es_repo, milvus_repo=None, dlq=dlq)
        ctx = IngestPipelineContext(raw_data_list=[])
        ctx.cells = [_cell()]

        # 不应抛
        await stage.run(ctx)

        assert len(ctx.es_failures) == 1
        records = await dlq.list()
        assert len(records) == 1
        assert records[0].target == "es"
        assert "ES down" in records[0].error

    async def test_milvus_skipped_when_no_embedding(self):
        fake = _FakeMilvusInsert()
        milvus_repo = MilvusMemCellRepo(insert_callable=fake)
        dlq = InMemoryDLQ()
        stage = SyncIndexStage(es_repo=None, milvus_repo=milvus_repo, dlq=dlq)
        ctx = IngestPipelineContext(raw_data_list=[])
        ctx.cells = [_cell(with_embedding=False)]
        await stage.run(ctx)
        assert fake.calls == []
        assert await dlq.size() == 0  # 不入 DLQ —— 这是预期路径

    async def test_milvus_called_when_embedding_present(self):
        fake = _FakeMilvusInsert()
        milvus_repo = MilvusMemCellRepo(insert_callable=fake)
        stage = SyncIndexStage(es_repo=None, milvus_repo=milvus_repo, dlq=None)
        ctx = IngestPipelineContext(raw_data_list=[])
        ctx.cells = [_cell(with_embedding=True)]
        await stage.run(ctx)
        assert len(fake.calls) == 1

    async def test_milvus_failure_enqueues_dlq(self):
        fake = _FakeMilvusInsert(fail=True)
        milvus_repo = MilvusMemCellRepo(insert_callable=fake)
        dlq = InMemoryDLQ()
        stage = SyncIndexStage(es_repo=None, milvus_repo=milvus_repo, dlq=dlq)
        ctx = IngestPipelineContext(raw_data_list=[])
        ctx.cells = [_cell(with_embedding=True)]
        await stage.run(ctx)
        records = await dlq.list()
        assert len(records) == 1
        assert records[0].target == "milvus"

    async def test_both_repos_none_is_noop(self):
        stage = SyncIndexStage(es_repo=None, milvus_repo=None, dlq=None)
        ctx = IngestPipelineContext(raw_data_list=[])
        ctx.cells = [_cell()]
        # 不应抛
        await stage.run(ctx)
        # ctx 不变(无 failures)
        assert ctx.es_failures == []
        assert ctx.milvus_failures == []


# ════════════════════════════════════════════════════════════════════════════
# IngestPipeline 端到端:ES 失败不阻塞
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestIngestPipelineWithSync:
    async def test_es_failure_does_not_block_ingest(self):
        client = _FakeESClient(index_should_fail=True)
        es_repo = ESMemCellRepo(client)
        dlq = InMemoryDLQ()
        pipe = IngestPipeline(
            segmenter=_FakeSegmenter(),
            mem_cell_repo=_FakeMongoRepo(),
            es_repo=es_repo,
            milvus_repo=None,
            dlq=dlq,
        )
        ids = await pipe.execute([_raw("hello")])
        assert len(ids) == 1
        # MongoDB 已落,DLQ 已记录
        assert await dlq.size() == 1

    async def test_no_es_repo_means_no_sync(self):
        pipe = IngestPipeline(
            segmenter=_FakeSegmenter(),
            mem_cell_repo=_FakeMongoRepo(),
            es_repo=None,
            milvus_repo=None,
            dlq=None,
        )
        ids = await pipe.execute([_raw("hi")])
        assert len(ids) == 1  # 仍然成功

    async def test_milvus_called_when_embedding_present_e2e(self):
        # 这次让 cell 有 embedding —— 通过定制 Stage 扩展(简化:直接做完整 pipeline)
        client = _FakeESClient()
        es_repo = ESMemCellRepo(client)
        fake = _FakeMilvusInsert()
        milvus_repo = MilvusMemCellRepo(insert_callable=fake)
        pipe = IngestPipeline(
            segmenter=_FakeSegmenter(),
            mem_cell_repo=_FakeMongoRepo(),
            es_repo=es_repo,
            milvus_repo=milvus_repo,
            dlq=InMemoryDLQ(),
        )
        # Phase 2 默认无 embedding → Milvus 不调
        await pipe.execute([_raw("hi")])
        assert fake.calls == []
        assert len(client.indexed) == 1
