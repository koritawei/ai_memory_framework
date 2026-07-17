"""E2E 测试装配：fake repo + 注入 ingest/retrieve/feedback 服务。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi.testclient import TestClient

from memory_app.deps import app_state
from memory_app.internal_models import MemCell, MemoryType, RankedMemory
from memory_app.pipelines import IngestPipeline
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.plugins_default.rule_sbd import RuleSBD
from memory_app.plugins_default.synaptic_reinforcer import SynapticPlasticityReinforcer
from memory_app.repositories.dlq import InMemoryDLQ
from memory_app.retrieval.fusion import RRFFusion
from memory_app.retrieval.orchestrator import RetrievalOrchestrator
from memory_app.retrieval.reranker import MMRReranker
from memory_app.services import FeedbackService, IngestService


class FakeMongoRepo:
    def __init__(self) -> None:
        self.store: dict[str, MemCell] = {}
        self.updates: list[tuple[str, dict]] = []

    async def insert(self, cell: MemCell) -> str:
        self.store[cell.mem_cell_id] = cell
        return cell.mem_cell_id

    async def get_by_id(self, mid: str) -> MemCell | None:
        return self.store.get(mid)

    async def update(self, mid: str, updates: dict) -> bool:
        if mid not in self.store:
            return False
        self.updates.append((mid, dict(updates)))
        cell = self.store[mid]
        for k, v in updates.items():
            setattr(cell, k, v)
        return True

    async def find_all(self, tenant_id: str, user_id: str, limit: int = 20):
        cells = [
            c
            for c in self.store.values()
            if c.tenant_id == tenant_id and c.user_id == user_id
        ]
        return cells[:limit]


class FakeESRepo:
    def __init__(self) -> None:
        self.indexed: list[str] = []

    async def index(self, cell: MemCell) -> None:
        self.indexed.append(cell.mem_cell_id)


def _query_matches(query: str, text: str) -> bool:
    """中文友好匹配：整句包含或任意 2-gram 命中。"""
    q = (query or "").strip().lower()
    t = (text or "").lower()
    if not q:
        return True
    if q in t:
        return True
    for i in range(len(q) - 1):
        if q[i : i + 2] in t:
            return True
    for tok in q.split():
        if len(tok) > 1 and tok in t:
            return True
    return False


class StubChannel:
    def __init__(self, name: str, store: dict[str, MemCell]) -> None:
        self.channel_name = name
        self._store = store

    async def retrieve(
        self, query: str, ctx: RetrievalContext, k: int
    ) -> list[RankedMemory]:
        hits: list[RankedMemory] = []
        for mid, cell in self._store.items():
            if cell.tenant_id != ctx.tenant_id or cell.user_id != ctx.user_id:
                continue
            text = cell.text or ""
            if _query_matches(query, text):
                hits.append(
                    RankedMemory(
                        memory_id=mid,
                        memory_type=MemoryType.EPISODIC,
                        content=cell.text or "",
                        score=0.9 if self.channel_name == "vector" else 5.0,
                        source_channel=self.channel_name,
                        metadata={"tenant_id": cell.tenant_id},
                    )
                )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


def build_ingest_pipeline(
    mongo: FakeMongoRepo,
    es: FakeESRepo | None = None,
    dlq: InMemoryDLQ | None = None,
) -> IngestPipeline:
    return IngestPipeline(
        segmenter=RuleSBD(),
        mem_cell_repo=mongo,
        es_repo=es or FakeESRepo(),
        milvus_repo=None,
        dlq=dlq,
    )


def build_orchestrator(mongo: FakeMongoRepo) -> RetrievalOrchestrator:
    return RetrievalOrchestrator(
        channels={
            "bm25": StubChannel("bm25", mongo.store),
            "vector": StubChannel("vector", mongo.store),
        },
        fuser=RRFFusion(),
        filters=[],
        reranker=MMRReranker(),
    )


@contextmanager
def wired_client(
    *,
    with_graph: bool = False,
) -> Iterator[tuple[TestClient, FakeMongoRepo, FakeESRepo]]:
    import asyncio

    from memory_app import api
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.settings import reset_settings_for_test

    reset_settings_for_test()
    reset_prompt_manager_for_test()

    mongo = FakeMongoRepo()
    es = FakeESRepo()
    pipeline = build_ingest_pipeline(mongo, es)
    reinforcer = SynapticPlasticityReinforcer()
    asyncio.run(reinforcer.start({}))

    with TestClient(api.app) as client:
        app_state.ingest_service = IngestService(pipeline)
        app_state.mongo_repo = mongo  # /v1/query/user-memories
        app_state.retrieval_orchestrator = build_orchestrator(mongo)
        app_state.feedback_service = FeedbackService(mongo_repo=mongo, reinforcer=reinforcer)
        if with_graph:
            from memory_app.entity_store import InMemoryEntityStore
            from memory_app.graph_index import InMemoryGraph, MemoryGraph

            app_state.entity_store = InMemoryEntityStore()
            app_state.memory_graph = MemoryGraph(InMemoryGraph())
        yield client, mongo, es

    reset_prompt_manager_for_test()


@contextmanager
def wired_consolidate_client() -> Iterator[tuple[TestClient, object]]:
    """仅装配 consolidate 端点（§7 离线认知）。"""
    from datetime import datetime, timezone

    from memory_app import api
    from memory_app.plugins.spi.consolidation_strategy import ConsolidationReport
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.services import ConsolidationService
    from memory_app.settings import reset_settings_for_test

    class _StubStrategy:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        async def run(self, scope="all", time=None):
            self.calls.append((scope, time))
            now = datetime.now(timezone.utc)
            return ConsolidationReport(
                phase="light" if scope == "all" else scope,
                started_at=now,
                finished_at=now,
                scanned_count=5,
                consolidated_count=2,
                archived_count=1,
            )

    reset_settings_for_test()
    reset_prompt_manager_for_test()
    strategy = _StubStrategy()
    with TestClient(api.app) as client:
        app_state.consolidation_service = ConsolidationService(strategy=strategy)
        yield client, strategy
    reset_prompt_manager_for_test()
