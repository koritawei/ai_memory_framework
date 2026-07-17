"""VectorStore SPI 契约测试(Phase 8 Step 8.4)。

═══════════════════════════════════════════════════════════════════════════════
目的
═══════════════════════════════════════════════════════════════════════════════
这是 SPI 等价性的"实际守门人":任何 ``VectorStore`` 实现(主仓 ``vector_milvus``、
第三方 ``qdrant_store`` 等)都必须通过本套用例;否则替换该实现会破坏业务平面
的隐式期待。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- ``upsert`` 是幂等的(相同 id 重复提交结果一致)
- ``search`` 索引未建立时返回空列表(**不**抛异常)
- ``search`` 维度一致时返回按 score 降序结果
- ``search`` filters 精确匹配 payload 字段(多租户隔离)
- ``upsert`` 维度不一致 → 抛 :class:`PluginError(category="config")`
- ``delete`` 返回实际删除数量
- ``flush`` 不抛
- 完整生命周期 ``start → upsert → search → stop`` 干净

═══════════════════════════════════════════════════════════════════════════════
覆盖的实现
═══════════════════════════════════════════════════════════════════════════════
- :class:`memory_plugin_qdrant.store.QdrantVectorStore`(``examples/`` 第三方插件)

主仓 ``vector_milvus`` 的契约测试由 ``test_vector_channel.py`` 走 mock;
真 Milvus 集成由 ops 维护的 docker compose 跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让契约测试能 import 到第三方插件包(等价于 pip install -e ./examples/...)
EXAMPLES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "examples"
    / "memory_plugin_qdrant"
)
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))


from memory_app.plugins.base import PluginError  # noqa: E402
from memory_app.plugins.spi.vector_store import (  # noqa: E402
    VectorItem,
    VectorStore,
)


@pytest.fixture
def store_factory():
    """每个 test 拿到一个全新的 store(避免测试相互污染)。

    生产可在此处 yield 多个实现(milvus / qdrant / pgvector)再用
    ``pytest.mark.parametrize`` 逐一跑契约。
    """
    def _make() -> VectorStore:
        from memory_plugin_qdrant.store import QdrantVectorStore

        return QdrantVectorStore()

    return _make


@pytest.mark.asyncio
class TestVectorStoreContract:
    COLLECTION = "memory_test_collection"

    async def _started(self, store_factory) -> VectorStore:
        store = store_factory()
        await store.start({"metric": "cosine"})
        return store

    async def test_health_after_start(self, store_factory):
        store = await self._started(store_factory)
        h = await store.health()
        assert h["status"] == "ok"
        await store.stop()

    async def test_health_before_start(self, store_factory):
        store = store_factory()
        h = await store.health()
        assert h["status"] == "fail"

    async def test_search_empty_returns_empty_not_raises(self, store_factory):
        store = await self._started(store_factory)
        hits = await store.search(self.COLLECTION, [0.1, 0.2, 0.3], k=5)
        assert hits == []
        await store.stop()

    async def test_upsert_then_search_top_k(self, store_factory):
        store = await self._started(store_factory)
        items = [
            VectorItem(id="a", vector=[1.0, 0.0, 0.0], payload={"tenant_id": "t1"}),
            VectorItem(id="b", vector=[0.9, 0.1, 0.0], payload={"tenant_id": "t1"}),
            VectorItem(id="c", vector=[0.0, 0.0, 1.0], payload={"tenant_id": "t1"}),
        ]
        await store.upsert(self.COLLECTION, items)

        hits = await store.search(self.COLLECTION, [1.0, 0.0, 0.0], k=2)
        assert len(hits) == 2
        # 按 score 降序;最相近的 a / b 在前
        assert {hits[0].id, hits[1].id} == {"a", "b"}
        assert hits[0].score >= hits[1].score
        await store.stop()

    async def test_upsert_is_idempotent(self, store_factory):
        store = await self._started(store_factory)
        item = VectorItem(id="x", vector=[1.0, 0.0])
        await store.upsert(self.COLLECTION, [item])
        await store.upsert(self.COLLECTION, [item])  # 重复
        await store.upsert(self.COLLECTION, [item])
        hits = await store.search(self.COLLECTION, [1.0, 0.0], k=10)
        assert len(hits) == 1  # 不重复
        await store.stop()

    async def test_filters_isolate_tenants(self, store_factory):
        store = await self._started(store_factory)
        await store.upsert(
            self.COLLECTION,
            [
                VectorItem(id="t1-a", vector=[1.0, 0.0], payload={"tenant_id": "t1"}),
                VectorItem(id="t2-a", vector=[1.0, 0.0], payload={"tenant_id": "t2"}),
            ],
        )
        hits = await store.search(
            self.COLLECTION, [1.0, 0.0], k=10, filters={"tenant_id": "t1"}
        )
        assert len(hits) == 1
        assert hits[0].id == "t1-a"
        await store.stop()

    async def test_dimension_mismatch_raises_config_error(self, store_factory):
        store = await self._started(store_factory)
        with pytest.raises(PluginError) as ei:
            await store.upsert(
                self.COLLECTION,
                [
                    VectorItem(id="x", vector=[1.0, 2.0]),
                    VectorItem(id="y", vector=[1.0, 2.0, 3.0]),  # 维度不一致
                ],
            )
        assert ei.value.category.value == "config"
        await store.stop()

    async def test_delete_returns_actual_count(self, store_factory):
        store = await self._started(store_factory)
        await store.upsert(
            self.COLLECTION,
            [
                VectorItem(id="a", vector=[1.0]),
                VectorItem(id="b", vector=[2.0]),
            ],
        )
        n = await store.delete(self.COLLECTION, ids=["a", "nonexistent"])
        assert n == 1
        # 再次删除 a 应返回 0
        n2 = await store.delete(self.COLLECTION, ids=["a"])
        assert n2 == 0
        await store.stop()

    async def test_flush_does_not_raise(self, store_factory):
        store = await self._started(store_factory)
        await store.flush(self.COLLECTION)
        await store.stop()


# ════════════════════════════════════════════════════════════════════════════
# Entry-points 注册检查:确保 pyproject.toml 写对
# ════════════════════════════════════════════════════════════════════════════
class TestThirdPartyPackageWiring:
    def test_class_has_required_meta(self):
        from memory_plugin_qdrant.store import QdrantVectorStore

        meta = QdrantVectorStore.meta
        assert meta.name == "qdrant_store"
        assert meta.category == "memory.storage.vector"
        assert meta.config_schema is not None

    def test_pyproject_declares_entry_point(self):
        # 直接读取 pyproject 文本确认 entry_point 行存在
        # ——> 把"插件如何 wiring"内化在测试中,改坏 pyproject.toml 会被立刻发现
        pyproject = (
            Path(__file__).resolve().parent.parent.parent
            / "examples"
            / "memory_plugin_qdrant"
            / "pyproject.toml"
        )
        body = pyproject.read_text(encoding="utf-8")
        assert "[project.entry-points.\"memory_app.plugins\"]" in body
        assert "qdrant_store = " in body
        assert "memory_plugin_qdrant.store:QdrantVectorStore" in body

    def test_class_inherits_from_spi(self):
        # 契约链:QdrantVectorStore < VectorStore < Plugin
        from memory_plugin_qdrant.store import QdrantVectorStore

        assert issubclass(QdrantVectorStore, VectorStore)
