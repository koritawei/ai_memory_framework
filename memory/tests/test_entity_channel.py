"""EntityChannel + entity_boost 插件测试。"""

from __future__ import annotations

import pytest

from memory_app.entity_store import InMemoryEntityStore
from memory_app.internal_models import MemCell
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.retrieval_channel import RetrievalContext
from memory_app.plugins_default.entity_boost_channel import EntityBoostChannel
from memory_app.plugins_default.regex_entity_extractor import RegexEntityExtractor
from memory_app.retrieval.channels.entity import EntityChannel, _fallback_tokenize


class _FakeMongoRepo:
    def __init__(self):
        self.store: dict[str, MemCell] = {}

    async def insert(self, cell):
        self.store[cell.mem_cell_id] = cell

    async def get_by_id(self, mid):
        return self.store.get(mid)


def _ctx() -> RetrievalContext:
    return RetrievalContext(tenant_id="t1", user_id="u1")


# ════════════════════════════════════════════════════════════════════════════
# fallback tokenize
# ════════════════════════════════════════════════════════════════════════════
class TestFallbackTokenize:
    def test_cjk_segments(self):
        out = _fallback_tokenize("我喜欢北京的烤鸭")
        # ≥2 字 CJK 段落
        assert any(len(t) >= 2 for t in out)

    def test_english_words(self):
        out = _fallback_tokenize("I love OpenAI tools")
        # ≥3 字英文
        assert "OpenAI" in out or "love" in out

    def test_dedupe(self):
        out = _fallback_tokenize("北京 北京 上海")
        # 大小写不敏感去重
        seen = [t.lower() for t in out]
        assert len(seen) == len(set(seen))

    def test_empty(self):
        assert _fallback_tokenize("") == []


# ════════════════════════════════════════════════════════════════════════════
# EntityChannel 核心
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestEntityChannelCore:
    async def _setup(self):
        repo = _FakeMongoRepo()
        await repo.insert(
            MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text="用户喜欢北京烤鸭", mem_cell_id="mc1",
            )
        )
        await repo.insert(
            MemCell(
                tenant_id="t1", user_id="u1", session_id="s1",
                text="北京天气很好", mem_cell_id="mc2",
            )
        )
        store = InMemoryEntityStore()
        await store.upsert_entities("mc1", ["北京", "烤鸭"], "t1", "u1")
        await store.upsert_entities("mc2", ["北京"], "t1", "u1")
        return repo, store

    async def test_search_returns_hits(self):
        repo, store = await self._setup()
        ch = EntityChannel(entity_store=store, mongo_repo=repo)
        hits = await ch.search("t1", "u1", "北京", top_k=10)
        assert len(hits) >= 2
        assert all(h.source_channel == "entity" for h in hits)

    async def test_search_no_entity_returns_empty(self):
        repo, store = await self._setup()
        ch = EntityChannel(entity_store=store, mongo_repo=repo)
        # query 不包含实体的 fallback 分词无结果
        hits = await ch.search("t1", "u1", " ", top_k=10)
        assert hits == []

    async def test_search_unrelated_query(self):
        repo, store = await self._setup()
        ch = EntityChannel(entity_store=store, mongo_repo=repo)
        hits = await ch.search("t1", "u1", "巴黎铁塔", top_k=10)
        assert hits == []

    async def test_search_score_by_match_count(self):
        repo, store = await self._setup()
        ch = EntityChannel(entity_store=store, mongo_repo=repo)
        # query 包含 "北京" + "烤鸭",mc1 命中 2,mc2 命中 1
        hits = await ch.search("t1", "u1", "北京 烤鸭", top_k=10)
        assert hits[0].memory_id == "mc1"
        assert hits[0].score >= hits[1].score

    async def test_unset_store_raises(self):
        ch = EntityChannel(entity_store=None, mongo_repo=_FakeMongoRepo())
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY

    async def test_unset_repo_raises(self):
        ch = EntityChannel(entity_store=InMemoryEntityStore(), mongo_repo=None)
        with pytest.raises(PluginError) as exc:
            await ch.search("t1", "u1", "x")
        assert exc.value.category == PluginErrorCategory.DEPENDENCY

    async def test_with_entity_extractor(self):
        repo, store = await self._setup()
        extractor = RegexEntityExtractor()
        await extractor.start({})
        ch = EntityChannel(
            entity_store=store, mongo_repo=repo, entity_extractor=extractor
        )
        # 用空格分隔让 RegexExtractor 单独抽出 "北京"
        hits = await ch.search("t1", "u1", "北京 美食", top_k=10)
        assert len(hits) >= 1


# ════════════════════════════════════════════════════════════════════════════
# entity_boost 插件
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestEntityBoostPlugin:
    async def test_unbound_raises(self):
        plugin = EntityBoostChannel()
        await plugin.start({})
        with pytest.raises(PluginError):
            await plugin.retrieve("北京", _ctx(), 5)

    async def test_bound_returns(self):
        plugin = EntityBoostChannel()
        await plugin.start({})
        repo = _FakeMongoRepo()
        await repo.insert(
            MemCell(tenant_id="t1", user_id="u1", session_id="s1",
                    text="北京烤鸭", mem_cell_id="mc1")
        )
        store = InMemoryEntityStore()
        await store.upsert_entities("mc1", ["北京"], "t1", "u1")
        plugin.bind_entity_store(store)
        plugin.bind_mongo_repo(repo)
        hits = await plugin.retrieve("北京", _ctx(), 5)
        assert len(hits) == 1
        assert hits[0].source_channel == "entity"

    async def test_health(self):
        plugin = EntityBoostChannel()
        await plugin.start({})
        assert (await plugin.health())["status"] == "degraded"
        plugin.bind_entity_store(InMemoryEntityStore())
        plugin.bind_mongo_repo(_FakeMongoRepo())
        assert (await plugin.health())["status"] == "ok"

    async def test_channel_name(self):
        plugin = EntityBoostChannel()
        await plugin.start({})
        assert plugin.channel_name == "entity"

    async def test_extractor_failure_falls_back_to_tokenize(self):
        class FailingExt:
            async def extract(self, _):
                raise RuntimeError("ext down")

        plugin = EntityBoostChannel()
        await plugin.start({})
        repo = _FakeMongoRepo()
        await repo.insert(MemCell(
            tenant_id="t1", user_id="u1", session_id="s1",
            text="北京", mem_cell_id="mc1",
        ))
        store = InMemoryEntityStore()
        await store.upsert_entities("mc1", ["北京"], "t1", "u1")
        plugin.bind_entity_store(store)
        plugin.bind_mongo_repo(repo)
        plugin.bind_entity_extractor(FailingExt())
        # 不应抛 —— extractor 失败时 fallback 到 tokenize
        hits = await plugin.retrieve("北京", _ctx(), 5)
        assert len(hits) == 1
