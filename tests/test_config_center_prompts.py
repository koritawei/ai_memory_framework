"""``PromptConfigMixin`` 端到端测试( / )。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- ``resolve_prompt`` 五级覆盖 + 灰度变体
- ``write_prompt`` 写入 + 版本递增 + history
- 简化语法糖(顶层 template 形态)在 default.yaml 与 variant 中均生效
- 内置种子 fallback (source=builtin)
- ``list_prompt_ids`` 含 builtin / 按 tag 过滤
- ``ConfigChangeEvent`` 在 write 后被通知
- ``validate_prompt_body`` 错误路径
- ``prompt_category`` / ``parse_prompt_id`` 边界条件
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from memory_app.config_center import (
    ConfigValidationError,
    FileConfigCenter,
    PromptNotFoundError,
    is_prompt_category,
    parse_prompt_id,
    prompt_category,
    validate_prompt_body,
)
from memory_app.prompt_manager.builtins import BUILTIN_PROMPTS


# ════════════════════════════════════════════════════════════════════════════
# fixtures
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    """构造一个含 prompt + plugin 的 default.yaml。"""
    p = tmp_path / "default.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "defaults": {
                    "memory": {
                        "retrieval": {
                            "fuser": {"name": "noop_fuser", "params": {"k": 60}}
                        },
                        "prompts": {
                            "episode_extraction": {
                                "description": "默认情景提取",
                                "version": "1.0.0",
                                "tags": ["generation", "episode"],
                                "variables": ["text"],
                                "template": "Default {text}",
                                "variants": [
                                    {
                                        "match": {"tenant_id_in": ["acme"]},
                                        "template": "Acme {text}",
                                    }
                                ],
                            },
                            "semantic_extraction": {
                                "tags": ["generation", "semantic"],
                                "variables": ["summary"],
                                "template": "Sem {summary}",
                            },
                        },
                    }
                },
                "global_overrides": {},
                "tenant_overrides": {},
                "user_overrides": {},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return p


@pytest.fixture
def cc(yaml_path: Path):
    """带 prompt 默认配置的 FileConfigCenter。"""
    # 触发默认插件注册(让 plugin schema 校验生效;prompt 不依赖)
    import memory_app.plugins_default  # noqa: F401

    return FileConfigCenter(yaml_path, poll_interval=10.0)


# ════════════════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════════════════
class TestPromptPaths:
    def test_prompt_category_basic(self):
        assert prompt_category("episode_extraction") == "memory.prompts.episode_extraction"

    def test_prompt_category_rejects_empty(self):
        with pytest.raises(ValueError):
            prompt_category("")

    def test_prompt_category_rejects_dotted_id(self):
        # 含 "." 会被 ConfigResolver 误判为多级 dotted key
        with pytest.raises(ValueError):
            prompt_category("foo.bar")

    def test_parse_prompt_id_round_trip(self):
        assert parse_prompt_id(prompt_category("x")) == "x"

    def test_parse_prompt_id_rejects_other_categories(self):
        assert parse_prompt_id("memory.generation.boundary_detector") is None

    def test_is_prompt_category(self):
        assert is_prompt_category("memory.prompts.foo")
        assert not is_prompt_category("memory.retrieval.fuser")


# ════════════════════════════════════════════════════════════════════════════
# Schema 校验
# ════════════════════════════════════════════════════════════════════════════
class TestValidatePromptBody:
    def test_valid_minimal(self):
        body = {"template": "hello {name}", "variables": ["name"]}
        out = validate_prompt_body(body)
        assert out["template"] == "hello {name}"

    def test_missing_template_raises(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_prompt_body({"variables": ["x"]})
        assert "/template" in exc.value.json_pointer

    def test_empty_template_raises(self):
        with pytest.raises(ConfigValidationError):
            validate_prompt_body({"template": "", "variables": []})

    def test_variables_must_be_list_of_str(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_prompt_body({"template": "x", "variables": [1, 2]})
        assert "/variables" in exc.value.json_pointer

    def test_variants_must_be_list(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_prompt_body(
                {"template": "x", "variants": {"match": {}}}
            )
        assert "/variants" in exc.value.json_pointer

    def test_variant_template_validated(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_prompt_body(
                {
                    "template": "x",
                    "variants": [{"match": {}, "template": ""}],
                }
            )
        assert "/variants/0/template" in exc.value.json_pointer

    def test_non_dict_body_raises(self):
        with pytest.raises(ConfigValidationError):
            validate_prompt_body("not a dict")  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════════════════
# resolve_prompt
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestResolvePrompt:
    async def test_resolve_default_layer(self, cc: FileConfigCenter):
        r = await cc.resolve_prompt("episode_extraction")
        assert r.template == "Default {text}"
        assert r.variables == ["text"]
        assert r.source == "default"
        assert r.tags == ["generation", "episode"]

    async def test_resolve_variant_by_tenant(self, cc: FileConfigCenter):
        # 简化语法糖:variant 顶层的 template 字段应生效
        r = await cc.resolve_prompt("episode_extraction", tenant_id="acme")
        assert r.template == "Acme {text}"
        assert r.source == "variant"

    async def test_resolve_non_acme_tenant_falls_back_to_default(
        self, cc: FileConfigCenter
    ):
        r = await cc.resolve_prompt("episode_extraction", tenant_id="globex")
        assert r.template == "Default {text}"
        assert r.source == "default"

    async def test_resolve_unknown_prompt_falls_back_to_builtin(
        self, cc: FileConfigCenter
    ):
        # default.yaml 只声明了 episode_extraction / semantic_extraction;
        # sbd_llm_refine 走内置种子
        r = await cc.resolve_prompt("sbd_llm_refine")
        assert r.source == "builtin"
        assert "boundary_index" in r.template
        assert "numbered_text" in r.variables

    async def test_resolve_completely_unknown_raises(self, cc: FileConfigCenter):
        with pytest.raises(PromptNotFoundError):
            await cc.resolve_prompt("not_existing_prompt_xyz")


# ════════════════════════════════════════════════════════════════════════════
# write_prompt + history
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestWritePrompt:
    async def test_write_global_then_resolve(self, cc: FileConfigCenter):
        new_v = await cc.write_prompt(
            "semantic_extraction",
            {"template": "v2 {summary}", "variables": ["summary"]},
            scope="global",
            actor="test",
        )
        assert new_v >= 1

        r = await cc.resolve_prompt("semantic_extraction")
        assert r.template == "v2 {summary}"
        assert r.source == "global"

    async def test_write_tenant_overrides_default(self, cc: FileConfigCenter):
        await cc.write_prompt(
            "episode_extraction",
            {"template": "TenantA {text}", "variables": ["text"]},
            scope="tenant",
            scope_id="tenant_a",
            actor="test",
        )
        r = await cc.resolve_prompt("episode_extraction", tenant_id="tenant_a")
        assert r.template == "TenantA {text}"
        assert r.source == "tenant"

    async def test_write_with_variants_takes_effect(self, cc: FileConfigCenter):
        await cc.write_prompt(
            "semantic_extraction",
            {
                "template": "Base {summary}",
                "variables": ["summary"],
                "variants": [
                    {
                        "match": {"tenant_id_in": ["acme"]},
                        "template": "Acme custom {summary}",
                    }
                ],
            },
            scope="global",
        )
        r_acme = await cc.resolve_prompt("semantic_extraction", tenant_id="acme")
        assert r_acme.template == "Acme custom {summary}"
        assert r_acme.source == "variant"

        r_other = await cc.resolve_prompt("semantic_extraction", tenant_id="other")
        assert r_other.template == "Base {summary}"

    async def test_write_invalid_body_raises(self, cc: FileConfigCenter):
        with pytest.raises(ConfigValidationError):
            await cc.write_prompt("foo", {"variables": ["x"]})  # 缺 template

    async def test_write_tenant_requires_scope_id(self, cc: FileConfigCenter):
        with pytest.raises(ValueError):
            await cc.write_prompt(
                "x", {"template": "t"}, scope="tenant", scope_id=None
            )

    async def test_write_invalid_scope_raises(self, cc: FileConfigCenter):
        with pytest.raises(ValueError):
            await cc.write_prompt("x", {"template": "t"}, scope="bogus")

    async def test_history_records_writes(self, cc: FileConfigCenter):
        await cc.write_prompt(
            "semantic_extraction",
            {"template": "v1 {summary}", "variables": ["summary"]},
            scope="global",
            actor="alice",
        )
        await cc.write_prompt(
            "semantic_extraction",
            {"template": "v2 {summary}", "variables": ["summary"]},
            scope="global",
            actor="bob",
        )
        hist = await cc.history_prompt("semantic_extraction", limit=5)
        assert len(hist) >= 2
        # 最新优先
        assert hist[0]["params"]["template"] == "v2 {summary}"
        assert hist[0]["actor"] == "bob"


# ════════════════════════════════════════════════════════════════════════════
# list_prompt_ids
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestListPromptIds:
    async def test_list_includes_default_and_builtin(self, cc: FileConfigCenter):
        ids = await cc.list_prompt_ids()
        # default.yaml 中的 + 内置种子的 union
        assert "episode_extraction" in ids
        assert "semantic_extraction" in ids
        assert "sbd_llm_refine" in ids  # 来自 builtin
        # 内置 user_preference_extract 也应在
        assert "user_preference_extract" in ids
        # 且去重
        assert len(ids) == len(set(ids))

    async def test_list_excludes_builtin_when_disabled(self, cc: FileConfigCenter):
        ids = await cc.list_prompt_ids(include_builtin=False)
        # 仅 default.yaml 中的两个
        assert "episode_extraction" in ids
        assert "semantic_extraction" in ids
        # builtin-only 的 user_preference_extract 不应在
        assert "user_preference_extract" not in ids

    async def test_list_with_tag_filter(self, cc: FileConfigCenter):
        ids = await cc.list_prompt_ids(tag="episode")
        assert "episode_extraction" in ids
        assert "semantic_extraction" not in ids

    async def test_list_with_unknown_tag_returns_empty(self, cc: FileConfigCenter):
        ids = await cc.list_prompt_ids(tag="totally_unknown_tag")
        assert ids == []


# ════════════════════════════════════════════════════════════════════════════
# 变更通知
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestPromptChangeNotify:
    async def test_write_prompt_notifies_watchers(self, cc: FileConfigCenter):
        events: list = []

        async def cb(event):
            events.append(event)

        await cc.watch(cb)
        await cc.write_prompt(
            "semantic_extraction",
            {"template": "new {summary}", "variables": ["summary"]},
            scope="global",
        )
        assert any(
            e.category == "memory.prompts.semantic_extraction" for e in events
        )

        # 检查事件载荷
        prompt_events = [
            e for e in events if e.category == "memory.prompts.semantic_extraction"
        ]
        assert prompt_events
        e = prompt_events[0]
        assert e.scope == "global"
        assert e.name == "semantic_extraction"
        assert e.version >= 1


# ════════════════════════════════════════════════════════════════════════════
# 内置种子完整性
# ════════════════════════════════════════════════════════════════════════════
class TestBuiltinPrompts:
    def test_builtin_count_matches_design(self):
        # 脚手架  的 5 个 + 离线巩固 新增 sleep_consolidation
        assert set(BUILTIN_PROMPTS.keys()) == {
            "episode_extraction",
            "episode_extraction_group_chat",
            "semantic_extraction",
            "sbd_llm_refine",
            "user_preference_extract",
            "sleep_consolidation",
        }

    def test_builtin_templates_have_required_placeholders(self):
        for pid, spec in BUILTIN_PROMPTS.items():
            for var in spec.variables:
                assert "{" + var + "}" in spec.template, (
                    f"builtin {pid}: template 缺占位符 {{{var}}}"
                )
