"""SemanticMemoryExtractor + LLM10AssociationExtractor 测试(Step 3.3)。"""

from __future__ import annotations

import pytest

from memory_app.extractors.semantic_extractor import (
    SemanticMemoryExtractor,
    parse_semantic_response,
)
from memory_app.internal_models import (
    EpisodicMemory,
    KnowledgeType,
    MemCell,
)
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins_default.llm_10_association import LLM10AssociationExtractor


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════
class _FakeLLM:
    def __init__(self, responses: list[str], fail: bool = False) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.fail = fail

    async def generate(self, prompt: str, **_) -> str:
        self.calls.append(prompt)
        if self.fail:
            raise RuntimeError("LLM down")
        if not self._responses:
            return "[]"
        return self._responses.pop(0)


def _episode(summary: str = "用户计划去北京出差") -> EpisodicMemory:
    return EpisodicMemory(
        episode_id="e1",
        mem_cell_id="mc1",
        tenant_id="t1",
        user_id="u1",
        summary=summary,
        key_entities=["北京", "出差"],
    )


def _cell(text: str = "我喜欢咖啡") -> MemCell:
    return MemCell(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        text=text,
    )


# ════════════════════════════════════════════════════════════════════════════
# 解析
# ════════════════════════════════════════════════════════════════════════════
class TestParseSemanticResponse:
    def test_array(self):
        items = parse_semantic_response('[{"content":"用户喜欢北京"}, {"content":"用户经常出差"}]')
        assert len(items) == 2

    def test_filters_empty_content(self):
        items = parse_semantic_response('[{"content":"ok"}, {"content":""}, {"content":"  "}]')
        assert len(items) == 1

    def test_invalid_returns_empty(self):
        assert parse_semantic_response("not json") == []
        assert parse_semantic_response("") == []
        assert parse_semantic_response("[]") == []


# ════════════════════════════════════════════════════════════════════════════
# 核心抽取器
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestSemanticMemoryExtractor:
    async def test_extract_for_episode(self):
        llm = _FakeLLM([
            '[{"content":"用户经常去北京出差","knowledge_type":"fact","confidence":0.9}]'
        ])
        extractor = SemanticMemoryExtractor(llm)
        sems = await extractor.extract(_episode())
        assert len(sems) == 1
        s = sems[0]
        assert "北京" in s.content
        assert s.knowledge_type == KnowledgeType.FACT
        assert s.confidence == 0.9
        assert s.source_episode_ids == ["e1"]
        assert s.source_memcell_ids == ["mc1"]

    async def test_extract_empty_response(self):
        llm = _FakeLLM(["[]"])
        extractor = SemanticMemoryExtractor(llm)
        sems = await extractor.extract(_episode())
        assert sems == []

    async def test_extract_multiple_filtered(self):
        llm = _FakeLLM([
            '[{"content":"a"},{"content":"b"},{"content":""}]'
        ])
        extractor = SemanticMemoryExtractor(llm)
        sems = await extractor.extract(_episode())
        # 空 content 已过滤
        assert len(sems) == 2

    async def test_max_items_truncates(self):
        llm = _FakeLLM([
            '[' + ",".join(['{"content":"x' + str(i) + '"}' for i in range(30)]) + ']'
        ])
        extractor = SemanticMemoryExtractor(llm, max_items=5)
        sems = await extractor.extract(_episode())
        assert len(sems) == 5

    async def test_unknown_knowledge_type_falls_back(self):
        llm = _FakeLLM(['[{"content":"x","knowledge_type":"weird"}]'])
        extractor = SemanticMemoryExtractor(llm)
        s = (await extractor.extract(_episode()))[0]
        assert s.knowledge_type == KnowledgeType.KNOWLEDGE  # fallback

    async def test_clamps_confidence(self):
        llm = _FakeLLM(['[{"content":"x","confidence":2.0}]'])
        extractor = SemanticMemoryExtractor(llm)
        s = (await extractor.extract(_episode()))[0]
        assert s.confidence == 1.0

    async def test_propagates_llm_error(self):
        llm = _FakeLLM([], fail=True)
        extractor = SemanticMemoryExtractor(llm)
        with pytest.raises(RuntimeError):
            await extractor.extract(_episode())

    async def test_extract_for_memcell(self):
        llm = _FakeLLM(['[{"content":"用户喜欢咖啡","knowledge_type":"preference"}]'])
        extractor = SemanticMemoryExtractor(llm)
        sems = await extractor.extract_for_memcell(_cell())
        assert len(sems) == 1
        assert sems[0].source_memcell_ids != []
        assert sems[0].source_episode_ids == []

    async def test_empty_summary_returns_empty(self):
        llm = _FakeLLM(['[{"content":"x"}]'])
        extractor = SemanticMemoryExtractor(llm)
        sems = await extractor.extract(_episode(summary=""))
        assert sems == []
        assert llm.calls == []


# ════════════════════════════════════════════════════════════════════════════
# 插件层
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestLLM10AssociationPlugin:
    async def test_unbound_raises(self):
        plugin = LLM10AssociationExtractor()
        await plugin.start({})
        with pytest.raises(PluginError) as exc_info:
            await plugin.extract_for_episode(_episode())
        assert exc_info.value.category == PluginErrorCategory.DEPENDENCY

    async def test_extract_for_episode(self):
        plugin = LLM10AssociationExtractor()
        await plugin.start({})
        plugin.bind_llm_client(_FakeLLM(['[{"content":"a"},{"content":"b"}]']))
        sems = await plugin.extract_for_episode(_episode())
        assert len(sems) == 2

    async def test_extract_for_memcell(self):
        plugin = LLM10AssociationExtractor()
        await plugin.start({})
        plugin.bind_llm_client(_FakeLLM(['[{"content":"a"}]']))
        sems = await plugin.extract_for_memcell(_cell())
        assert len(sems) == 1

    async def test_max_items_config(self):
        plugin = LLM10AssociationExtractor()
        await plugin.start({"max_items": 3})
        plugin.bind_llm_client(
            _FakeLLM(['[' + ",".join(['{"content":"x' + str(i) + '"}' for i in range(20)]) + ']'])
        )
        sems = await plugin.extract_for_episode(_episode())
        assert len(sems) == 3

    async def test_llm_failure_wrapped_as_plugin_error(self):
        plugin = LLM10AssociationExtractor()
        await plugin.start({})
        plugin.bind_llm_client(_FakeLLM([], fail=True))
        with pytest.raises(PluginError) as exc_info:
            await plugin.extract_for_episode(_episode())
        assert exc_info.value.retryable is True
