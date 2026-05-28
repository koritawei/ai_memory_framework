"""EpisodeMemoryExtractor + LLMEpisodeExtractor 测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memory_app.extractors.episode_extractor import (
    EpisodeMemoryExtractor,
    parse_episode_response,
)
from memory_app.internal_models import MemCell
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.episode_extractor import ScenarioType
from memory_app.plugins_default.llm_episode_extractor import LLMEpisodeExtractor


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


def _cell(text: str = "我下周要去北京出差", **overrides) -> MemCell:
    base = dict(
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        text=text,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return MemCell(**base)


# ════════════════════════════════════════════════════════════════════════════
# 解析
# ════════════════════════════════════════════════════════════════════════════
class TestParseEpisodeResponse:
    def test_array(self):
        items = parse_episode_response('[{"summary": "去北京"}]')
        assert len(items) == 1
        assert items[0]["summary"] == "去北京"

    def test_single_dict(self):
        items = parse_episode_response('{"summary": "去北京"}')
        assert len(items) == 1

    def test_code_fence(self):
        items = parse_episode_response('```json\n[{"summary": "x"}]\n```')
        assert len(items) == 1

    def test_invalid_returns_empty(self):
        assert parse_episode_response("not json") == []

    def test_empty(self):
        assert parse_episode_response("") == []
        assert parse_episode_response("[]") == []

    def test_filters_non_dict_items(self):
        items = parse_episode_response('[{"summary": "ok"}, "junk", null]')
        assert len(items) == 1


# ════════════════════════════════════════════════════════════════════════════
# 核心抽取器
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestEpisodeMemoryExtractor:
    async def test_extract_success_array(self):
        llm = _FakeLLM([
            '[{"summary":"用户计划去北京出差","key_entities":["北京"],"emotional_valence":0.3,"importance":0.7}]'
        ])
        extractor = EpisodeMemoryExtractor(llm)
        eps = await extractor.extract(_cell(), scenario=ScenarioType.ASSISTANT)
        assert len(eps) == 1
        ep = eps[0]
        assert "北京" in ep.summary
        assert ep.key_entities == ["北京"]
        assert ep.emotional_valence == 0.3
        assert ep.importance_score == 0.7
        assert ep.tenant_id == "t1" and ep.user_id == "u1"

    async def test_extract_multiple_episodes(self):
        llm = _FakeLLM([
            '[{"summary":"a"}, {"summary":"b"}, {"summary":"c"}]'
        ])
        extractor = EpisodeMemoryExtractor(llm)
        eps = await extractor.extract(_cell())
        assert len(eps) == 3

    async def test_extract_bad_json_falls_back(self):
        llm = _FakeLLM(["not json"])
        extractor = EpisodeMemoryExtractor(llm)
        eps = await extractor.extract(_cell())
        # 解析失败兜底:仍产出 1 条情景,summary=cell.text 前 50 字
        assert len(eps) == 1
        assert eps[0].summary  # 非空

    async def test_empty_cell_returns_empty(self):
        llm = _FakeLLM(["[]"])
        extractor = EpisodeMemoryExtractor(llm)
        eps = await extractor.extract(_cell(text=""))
        assert eps == []
        assert llm.calls == []  # 空 cell 不触发 LLM

    async def test_propagates_llm_error(self):
        llm = _FakeLLM([], fail=True)
        extractor = EpisodeMemoryExtractor(llm)
        with pytest.raises(RuntimeError, match="LLM down"):
            await extractor.extract(_cell())

    async def test_uses_group_chat_prompt_id(self):
        """scenario=GROUP_CHAT 应使用 episode_extraction_group_chat 模板。"""
        llm = _FakeLLM(['[{"summary":"x","participants":["alice","bob"]}]'])
        extractor = EpisodeMemoryExtractor(llm)
        cell = _cell(participants=["alice", "bob"])
        eps = await extractor.extract(cell, scenario=ScenarioType.GROUP_CHAT)
        assert len(eps) == 1

    async def test_clamps_emotional_valence(self):
        llm = _FakeLLM(['[{"summary":"x","emotional_valence":2.0}]'])
        extractor = EpisodeMemoryExtractor(llm)
        ep = (await extractor.extract(_cell()))[0]
        assert ep.emotional_valence == 1.0  # 上限截断

    async def test_string_entities_split_by_comma(self):
        llm = _FakeLLM(['[{"summary":"x","key_entities":"北京, 出差,周一"}]'])
        extractor = EpisodeMemoryExtractor(llm)
        ep = (await extractor.extract(_cell()))[0]
        assert ep.key_entities == ["北京", "出差", "周一"]


# ════════════════════════════════════════════════════════════════════════════
# 插件层
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestLLMEpisodeExtractorPlugin:
    async def test_unbound_raises(self):
        plugin = LLMEpisodeExtractor()
        await plugin.start({})
        with pytest.raises(PluginError) as exc_info:
            await plugin.extract(_cell())
        assert exc_info.value.category == PluginErrorCategory.DEPENDENCY
        assert exc_info.value.retryable is True

    async def test_bound_extracts(self):
        plugin = LLMEpisodeExtractor()
        await plugin.start({})
        plugin.bind_llm_client(_FakeLLM(['[{"summary":"测试"}]']))
        eps = await plugin.extract(_cell())
        assert len(eps) == 1

    async def test_llm_error_wrapped_as_plugin_error(self):
        plugin = LLMEpisodeExtractor()
        await plugin.start({})
        plugin.bind_llm_client(_FakeLLM([], fail=True))
        with pytest.raises(PluginError) as exc_info:
            await plugin.extract(_cell())
        assert exc_info.value.category == PluginErrorCategory.DEPENDENCY
        assert exc_info.value.retryable is True

    async def test_health_reflects_binding(self):
        plugin = LLMEpisodeExtractor()
        await plugin.start({})
        h = await plugin.health()
        assert h["status"] == "degraded"
        plugin.bind_llm_client(_FakeLLM(['[]']))
        h = await plugin.health()
        assert h["status"] == "ok"
