"""RegexEntityExtractor 测试(Step 7.1 配套插件)。"""

from __future__ import annotations

import pytest

from memory_app.plugins.spi.entity_extractor import EntityType
from memory_app.plugins_default.regex_entity_extractor import (
    DEFAULT_STOPWORDS,
    RegexEntityExtractor,
)


@pytest.mark.asyncio
class TestRegexEntityExtractor:
    async def test_extract_cjk_entities(self):
        p = RegexEntityExtractor()
        await p.start({})
        # 空格分隔让贪婪正则把每个 CJK 段独立提取
        out = await p.extract("我 下周 要去 北京 出差 美食")
        texts = {e.text for e in out}
        assert "北京" in texts
        assert "美食" in texts

    async def test_extract_english_proper(self):
        p = RegexEntityExtractor()
        await p.start({})
        out = await p.extract("I work at OpenAI in San Francisco")
        texts = {e.text for e in out}
        # PROPER 应捕获多词专名(顺序敏感的正则)
        assert "OpenAI" in texts
        # San Francisco 应作为合成专名(注意正则可能拆开)
        assert "San Francisco" in texts or "Francisco" in texts

    async def test_extract_quoted(self):
        p = RegexEntityExtractor()
        await p.start({})
        out = await p.extract('He said "Project Alpha" was canceled')
        texts = {e.text for e in out}
        assert "Project Alpha" in texts
        # QUOTED 优先级最高,应在结果靠前
        for e in out:
            if e.text == "Project Alpha":
                assert e.entity_type == EntityType.QUOTED
                break

    async def test_stopwords_filtered(self):
        p = RegexEntityExtractor()
        await p.start({})
        out = await p.extract("we have a thing for users today")
        texts = {e.text.lower() for e in out}
        assert "thing" not in texts
        assert "today" not in texts
        assert "users" not in texts

    async def test_min_length(self):
        p = RegexEntityExtractor()
        await p.start({"min_length": 3})
        out = await p.extract("AI 北京 a")
        # "AI" / "a" 长度 < 3 → 被过滤
        for e in out:
            assert len(e.text) >= 3

    async def test_max_entities_truncates(self):
        p = RegexEntityExtractor()
        await p.start({"max_entities_per_text": 2})
        out = await p.extract("北京 上海 广州 深圳")
        assert len(out) <= 2

    async def test_dedupe_normalized(self):
        p = RegexEntityExtractor()
        await p.start({})
        # 重复同一专名 → 去重
        out = await p.extract("OpenAI is great. OpenAI is awesome.")
        names = [e.text for e in out if e.text.lower() == "openai"]
        assert len(names) == 1

    async def test_substring_dropped_within_same_type(self):
        p = RegexEntityExtractor()
        await p.start({})
        out = await p.extract("机场高速 高速")
        texts = {e.text for e in out}
        # "高速" 是 "机场高速" 的子串(同 COMPOUND 类型)→ 被丢
        if "机场高速" in texts:
            assert "高速" not in texts

    async def test_extract_batch(self):
        p = RegexEntityExtractor()
        await p.start({})
        out = await p.extract_batch(["北京 上海", "OpenAI"])
        assert len(out) == 2

    async def test_empty_input(self):
        p = RegexEntityExtractor()
        await p.start({})
        assert await p.extract("") == []
        assert await p.extract("   ") == []

    async def test_health(self):
        p = RegexEntityExtractor()
        await p.start({})
        h = await p.health()
        assert h["status"] == "ok"
        assert "stopwords" in h["detail"]

    async def test_extra_stopwords(self):
        p = RegexEntityExtractor()
        await p.start({"extra_stopwords": ["北京"]})
        out = await p.extract("北京 上海")
        texts = {e.text for e in out}
        assert "北京" not in texts
        assert "上海" in texts


def test_default_stopwords_nonempty():
    assert "thing" in DEFAULT_STOPWORDS
    assert "今天" in DEFAULT_STOPWORDS
