"""幂等 claim + Milvus upsert + 分布式锁 单元测试。"""

from __future__ import annotations

import pytest

from memory_app.concurrency import RedisDistributedLock
from memory_app.repositories.idempotency import (
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
)
from memory_app.repositories.milvus_repo import MilvusMemCellRepo
from memory_app.services import IngestService
from tests.fixtures.fake_redis import FakeRedisLists


@pytest.mark.asyncio
async def test_in_memory_idempotency_claim_and_complete():
    store = InMemoryIdempotencyStore()
    first = await store.claim("k1", {"status": "processing"})
    assert first.claimed is True
    second = await store.claim("k1", {"status": "processing"})
    assert second.claimed is False
    await store.complete("k1", {"status": "done", "mem_cell_ids": ["a"]})
    third = await store.claim("k1", {"status": "processing"})
    assert third.claimed is False
    assert third.existing_value["mem_cell_ids"] == ["a"]


@pytest.mark.asyncio
async def test_redis_idempotency_store():
    redis = FakeRedisLists()
    store = RedisIdempotencyStore(redis)
    first = await store.claim("k2", {"status": "processing"})
    assert first.claimed is True
    second = await store.claim("k2", {"status": "x"})
    assert second.claimed is False


@pytest.mark.asyncio
async def test_ingest_service_idempotency_short_circuits():
    class _Pipe:
        async def run_to_context(self, _raw):
            raise AssertionError("should not run pipeline on idempotent replay")

    store = InMemoryIdempotencyStore()
    await store.claim("ingest:t1:u1:ik", {"status": "done", "mem_cell_ids": ["m1"]})
    # overwrite with complete semantics
    await store.complete("ingest:t1:u1:ik", {"status": "done", "mem_cell_ids": ["m1"]})
    svc = IngestService(_Pipe(), idempotency_store=store)
    ids = await svc.ingest(
        [{"x": 1}],  # type: ignore[arg-type]
        idempotency_key="ik",
        tenant_id="t1",
        user_id="u1",
    )
    assert ids == ["m1"]


@pytest.mark.asyncio
async def test_milvus_upsert_deletes_then_inserts():
    calls: list[tuple[str, object]] = []

    async def insert_cb(mid, emb, meta):
        calls.append(("insert", mid, emb))

    async def delete_cb(mid):
        calls.append(("delete", mid))

    repo = MilvusMemCellRepo(
        insert_callable=insert_cb, delete_callable=delete_cb
    )
    await repo.upsert("c1", [0.1, 0.2], metadata={"tenant_id": "t"})
    assert calls[0] == ("delete", "c1")
    assert calls[1][0] == "insert"
    assert calls[1][1] == "c1"


@pytest.mark.asyncio
async def test_redis_distributed_lock_mutual_exclusion():
    redis = FakeRedisLists()
    a = RedisDistributedLock(redis, "lock:test", ttl_s=30)
    b = RedisDistributedLock(redis, "lock:test", ttl_s=30)
    assert await a.acquire() is True
    assert await b.acquire() is False
    await a.release()
    assert await b.acquire() is True
    await b.release()
