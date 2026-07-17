"""IngestPipeline + IngestService 测试(Step 2.2)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory_app.internal_models import MemCell, RawData
from memory_app.pipelines import IngestPipeline, IngestPipelineContext
from memory_app.pipelines.base import PipelineStage
from memory_app.services import IngestService


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeSegmenter:
    """对 raws 按"奇数索引切"做模拟切分,便于控制 segment 数。"""

    def __init__(self, fixed_segments: list[list[RawData]] | None = None):
        self._fixed = fixed_segments

    async def segment(self, raw_data_list: list[RawData]) -> list[list[RawData]]:
        if self._fixed is not None:
            return self._fixed
        if not raw_data_list:
            return []
        return [raw_data_list]


class _FakeMongoRepo:
    def __init__(self) -> None:
        self.store: dict[str, MemCell] = {}
        self.insert_calls: list[MemCell] = []

    async def insert(self, cell: MemCell) -> str:
        self.insert_calls.append(cell)
        self.store[cell.mem_cell_id] = cell
        return cell.mem_cell_id

    async def get_by_id(self, mid: str) -> MemCell | None:
        return self.store.get(mid)


def _raw(content: str, minutes: int = 0) -> RawData:
    return RawData(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        content=content,
        event_time=datetime(2026, 1, 1, tzinfo=timezone.utc)
        + timedelta(minutes=minutes),
    )


# ════════════════════════════════════════════════════════════════════════════
# IngestPipeline 阶段测试
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestIngestPipeline:
    async def test_empty_input_returns_empty(self):
        pipe = IngestPipeline(
            segmenter=_FakeSegmenter(), mem_cell_repo=_FakeMongoRepo()
        )
        ctx = await pipe.build_context([])
        assert ctx.raw_data_list == []
        ids = await pipe.execute([])
        assert ids == []

    async def test_single_segment_creates_one_cell(self):
        repo = _FakeMongoRepo()
        pipe = IngestPipeline(segmenter=_FakeSegmenter(), mem_cell_repo=repo)
        raws = [_raw("hello"), _raw("world", 5)]
        ids = await pipe.execute(raws)
        assert len(ids) == 1
        cell = repo.store[ids[0]]
        # text 拼接
        assert "hello" in cell.text and "world" in cell.text
        # raw_data_ids 全部记录溯源
        assert len(cell.raw_data_ids) == 2

    async def test_multiple_segments_create_multiple_cells(self):
        # 强制让 segmenter 切成 2 段
        raws = [_raw("a"), _raw("b", 60), _raw("c", 65)]
        segmenter = _FakeSegmenter(fixed_segments=[[raws[0]], raws[1:]])
        repo = _FakeMongoRepo()
        pipe = IngestPipeline(segmenter=segmenter, mem_cell_repo=repo)
        ids = await pipe.execute(raws)
        assert len(ids) == 2
        # 顺序与 segments 顺序一致
        assert repo.store[ids[0]].text == "a"
        assert "b" in repo.store[ids[1]].text and "c" in repo.store[ids[1]].text

    async def test_metrics_recorded(self):
        repo = _FakeMongoRepo()
        pipe = IngestPipeline(segmenter=_FakeSegmenter(), mem_cell_repo=repo)
        # 跑一次 execute 后,通过手工 build_context + run 检查 metrics
        raws = [_raw("x")]
        ctx = await pipe.build_context(raws)
        for stage in pipe.stages():
            ctx = await stage.run(ctx)
        assert ctx.metrics["raw_count"] == 1
        assert ctx.metrics["segment_count"] == 1
        assert ctx.metrics["persisted_count"] == 1

    async def test_skips_empty_segments(self):
        """SegmenterStage 输出含空 segment 时 PersistStage 不应崩。"""
        repo = _FakeMongoRepo()
        # 注入一个返回空段的 segmenter
        segmenter = _FakeSegmenter(fixed_segments=[[], [_raw("only")], []])
        pipe = IngestPipeline(segmenter=segmenter, mem_cell_repo=repo)
        ids = await pipe.execute([_raw("only")])
        assert len(ids) == 1
        assert repo.store[ids[0]].text == "only"

    async def test_extra_stage_appended(self):
        """额外 Stage 可被插入并按序执行。"""
        captured: list[str] = []

        class TagStage(PipelineStage[IngestPipelineContext]):
            name = "tag"

            async def run(self, ctx):
                captured.append("tag_called")
                ctx.metrics["tagged"] = True
                return ctx

        repo = _FakeMongoRepo()
        pipe = IngestPipeline(
            segmenter=_FakeSegmenter(),
            mem_cell_repo=repo,
            extra_stages=[TagStage()],
        )
        await pipe.execute([_raw("x")])
        assert captured == ["tag_called"]

    async def test_should_skip_stage_default_false(self):
        repo = _FakeMongoRepo()
        pipe = IngestPipeline(segmenter=_FakeSegmenter(), mem_cell_repo=repo)
        ctx = IngestPipelineContext(raw_data_list=[])
        for stage in pipe.stages():
            assert (await pipe.should_skip_stage(stage, ctx)) is False


# ════════════════════════════════════════════════════════════════════════════
# IngestService 门面测试
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestIngestService:
    async def test_ingest_delegates_to_pipeline(self):
        repo = _FakeMongoRepo()
        pipe = IngestPipeline(
            segmenter=_FakeSegmenter(), mem_cell_repo=repo
        )
        service = IngestService(pipeline=pipe)
        raws = [_raw("hi"), _raw("there")]
        ids = await service.ingest(raws)
        assert len(ids) == 1
        assert ids[0] in repo.store

    async def test_ingest_empty_short_circuit(self):
        repo = _FakeMongoRepo()
        pipe = IngestPipeline(
            segmenter=_FakeSegmenter(), mem_cell_repo=repo
        )
        service = IngestService(pipeline=pipe)
        ids = await service.ingest([])
        assert ids == []
        # 空列表时不应触达 repo
        assert repo.insert_calls == []
