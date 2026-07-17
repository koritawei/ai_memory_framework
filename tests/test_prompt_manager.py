"""PromptManager + Admin 路由测试(Step 0.7)。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- ``StandalonePromptManager``        本地渲染、缺占位符报错
- ``ConfigCenterPromptManager``      委托 ConfigCenter + 缓存 + watch 失效
- ``init_prompt_manager`` 单例幂等
- Admin 路由 6 端点(GET 列表 / GET 单条 / GET history / PUT / DELETE / POST render)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from memory_app.config_center import FileConfigCenter
from memory_app.prompt_manager import (
    BUILTIN_PROMPTS,
    ConfigCenterPromptManager,
    PromptSpec,
    StandalonePromptManager,
)


# ════════════════════════════════════════════════════════════════════════════
# StandalonePromptManager
# ════════════════════════════════════════════════════════════════════════════
class TestStandalonePromptManager:
    def test_resolve_builtin(self):
        mgr = StandalonePromptManager()
        r = mgr.resolve("episode_extraction")
        assert r.source == "builtin"
        assert "text" in r.variables

    def test_resolve_unknown_raises(self):
        mgr = StandalonePromptManager(include_builtin=False)
        with pytest.raises(KeyError):
            mgr.resolve("episode_extraction")

    def test_register_overrides_builtin(self):
        mgr = StandalonePromptManager()
        mgr.register(
            "episode_extraction",
            PromptSpec(template="custom {text}", variables=["text"]),
        )
        r = mgr.resolve("episode_extraction")
        assert r.template == "custom {text}"
        assert r.source == "override"

    def test_register_with_dict(self):
        mgr = StandalonePromptManager()
        mgr.register(
            "x", {"template": "hi {name}", "variables": ["name"]}
        )
        assert mgr.resolve("x").template == "hi {name}"

    def test_render_basic(self):
        mgr = StandalonePromptManager()
        mgr.register("g", {"template": "hi {name}", "variables": ["name"]})
        assert mgr.render("g", name="alice") == "hi alice"

    def test_render_missing_variable_raises(self):
        mgr = StandalonePromptManager()
        mgr.register("g", {"template": "hi {name}", "variables": ["name"]})
        with pytest.raises(ValueError) as exc:
            mgr.render("g")
        assert "missing prompt variables" in str(exc.value)

    def test_render_template_undeclared_var_raises(self):
        mgr = StandalonePromptManager()
        # template 含 {age} 但 variables 未声明 → 调用方传 name 但不传 age
        mgr.register("g", {"template": "hi {name} {age}", "variables": ["name"]})
        with pytest.raises(ValueError):
            mgr.render("g", name="alice")

    @pytest.mark.asyncio
    async def test_render_for_ignores_tenant_user(self):
        # StandalonePromptManager 不参与灰度,但接口要兼容
        mgr = StandalonePromptManager()
        mgr.register("g", {"template": "hi {name}", "variables": ["name"]})
        out = await mgr.render_for("g", tenant_id="acme", user_id="u1", name="x")
        assert out == "hi x"

    def test_list_prompts_includes_overrides(self):
        mgr = StandalonePromptManager()
        mgr.register("custom_prompt", {"template": "T", "variables": []})
        ids = mgr.list_prompts()
        assert "custom_prompt" in ids
        # 内置也在
        assert all(pid in ids for pid in BUILTIN_PROMPTS)


# ════════════════════════════════════════════════════════════════════════════
# ConfigCenterPromptManager
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def cc_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "default.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "defaults": {
                    "memory": {
                        "prompts": {
                            "episode_extraction": {
                                "variables": ["text"],
                                "template": "Default {text}",
                                "tags": ["episode"],
                                "variants": [
                                    {
                                        "match": {"tenant_id_in": ["acme"]},
                                        "template": "Acme {text}",
                                    }
                                ],
                            }
                        }
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
async def cc(cc_yaml: Path):
    """带 prompt 配置的 FileConfigCenter。"""
    import memory_app.plugins_default  # noqa: F401

    cc_inst = FileConfigCenter(cc_yaml, poll_interval=10.0)
    yield cc_inst
    await cc_inst.close()


@pytest.mark.asyncio
class TestConfigCenterPromptManager:
    async def test_resolve_default(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        r = await mgr.resolve("episode_extraction")
        assert r.template == "Default {text}"
        assert r.source == "default"

    async def test_resolve_variant_by_tenant(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        r = await mgr.resolve("episode_extraction", tenant_id="acme")
        assert r.template == "Acme {text}"
        assert r.source == "variant"

    async def test_render_for(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        out = await mgr.render_for(
            "episode_extraction", tenant_id="acme", text="hello"
        )
        assert out == "Acme hello"

    async def test_render_missing_var_raises(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        with pytest.raises(ValueError):
            await mgr.render_for("episode_extraction")

    async def test_cache_hit(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        r1 = await mgr.resolve("episode_extraction", tenant_id="acme")
        r2 = await mgr.resolve("episode_extraction", tenant_id="acme")
        # 同一缓存键应返回同一实例
        assert r1 is r2

    async def test_watch_invalidates_cache_on_prompt_write(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        await mgr.attach_watcher()

        r1 = await mgr.resolve("episode_extraction")
        assert r1.template == "Default {text}"

        # 写入新版 → 应触发缓存失效
        await cc.write_prompt(
            "episode_extraction",
            {"template": "Updated {text}", "variables": ["text"]},
            scope="global",
        )
        # 让 watcher 任务跑一拍
        import asyncio

        await asyncio.sleep(0)

        r2 = await mgr.resolve("episode_extraction")
        assert r2.template == "Updated {text}"
        assert r2 is not r1  # 缓存被刷新

    async def test_attach_watcher_idempotent(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        await mgr.attach_watcher()
        await mgr.attach_watcher()  # 不应重复 watch
        # 仅打 1 个 callback
        assert len(cc._callbacks) == 1

    async def test_invalidate_cache_specific_id(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        await mgr.resolve("episode_extraction")
        assert ("episode_extraction", "*", "*") in mgr._cache

        await mgr.invalidate_cache("episode_extraction")
        assert ("episode_extraction", "*", "*") not in mgr._cache

    async def test_list_prompts_delegates_to_cc(self, cc: FileConfigCenter):
        mgr = ConfigCenterPromptManager(cc)
        ids = await mgr.list_prompts()
        assert "episode_extraction" in ids


# ════════════════════════════════════════════════════════════════════════════
# init_prompt_manager 单例
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
class TestPromptRuntimeAsync:
    """init_prompt_manager 异步路径。"""

    async def test_init_returns_config_backed(self, cc: FileConfigCenter):
        from memory_app.prompt_runtime import (
            get_prompt_manager,
            init_prompt_manager,
            reset_prompt_manager_for_test,
        )

        reset_prompt_manager_for_test()
        try:
            mgr = await init_prompt_manager(cc)
            assert isinstance(mgr, ConfigCenterPromptManager)
            assert get_prompt_manager() is mgr
        finally:
            reset_prompt_manager_for_test()

    async def test_init_idempotent(self, cc: FileConfigCenter):
        from memory_app.prompt_runtime import (
            init_prompt_manager,
            reset_prompt_manager_for_test,
        )

        reset_prompt_manager_for_test()
        try:
            m1 = await init_prompt_manager(cc)
            m2 = await init_prompt_manager(cc)
            assert m1 is m2
        finally:
            reset_prompt_manager_for_test()


class TestPromptRuntimeSync:
    """同步回退路径(无 ConfigCenter 时)。"""

    def test_get_falls_back_to_standalone(self):
        from memory_app.prompt_runtime import (
            get_prompt_manager,
            reset_prompt_manager_for_test,
        )

        reset_prompt_manager_for_test()
        try:
            mgr = get_prompt_manager()
            assert isinstance(mgr, StandalonePromptManager)
        finally:
            reset_prompt_manager_for_test()


# ════════════════════════════════════════════════════════════════════════════
# Admin 路由(端到端 TestClient)
#
# 关键:把 default.yaml 复制到 tmp_path,通过 MEMORY_CONFIG_CENTER_FILE_PATH
# 重定向 ConfigCenter 到副本——避免 PUT/DELETE 污染仓库的 default.yaml。
# ════════════════════════════════════════════════════════════════════════════
import shutil


@pytest.fixture
def isolated_default_yaml(tmp_path: Path, monkeypatch) -> Path:
    """复制仓库 default.yaml 到 tmp_path,并重定向 ConfigCenter 到副本。"""
    project_root = Path(__file__).resolve().parent.parent
    src = project_root / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    shutil.copy2(src, dst)
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    return dst


@pytest.fixture
def project_cwd():
    cwd = os.getcwd()
    os.chdir(Path(__file__).resolve().parent.parent)
    try:
        yield
    finally:
        os.chdir(cwd)


@pytest.fixture
def admin_client(project_cwd, isolated_default_yaml, monkeypatch):
    """启动应用,使用临时 ``config/default.yaml`` 副本(避免污染仓库文件)。"""
    from memory_app import api
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.settings import reset_settings_for_test

    reset_settings_for_test()
    reset_prompt_manager_for_test()
    with TestClient(api.app) as c:
        yield c
    reset_prompt_manager_for_test()


class TestAdminPromptRoutes:
    """Admin /v1/admin/prompts 路由端到端。"""

    def test_list_prompts(self, admin_client: TestClient):
        r = admin_client.get("/v1/admin/prompts")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["prompts"], list)
        # 仓库 default.yaml 至少含这几个
        assert "episode_extraction" in body["prompts"]
        assert "semantic_extraction" in body["prompts"]
        assert body["total"] == len(body["prompts"])

    def test_list_with_tag(self, admin_client: TestClient):
        r = admin_client.get("/v1/admin/prompts?tag=episode")
        assert r.status_code == 200
        ids = r.json()["prompts"]
        assert "episode_extraction" in ids

    def test_get_prompt_default(self, admin_client: TestClient):
        r = admin_client.get("/v1/admin/prompts/episode_extraction")
        assert r.status_code == 200
        body = r.json()
        assert body["prompt_id"] == "episode_extraction"
        assert body["template"]
        assert body["source"] in ("default", "builtin")

    def test_get_prompt_with_acme_variant(self, admin_client: TestClient):
        r = admin_client.get(
            "/v1/admin/prompts/episode_extraction?tenant_id=acme"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "variant"
        assert "Acme" in body["template"]

    def test_get_unknown_returns_404(self, admin_client: TestClient):
        r = admin_client.get("/v1/admin/prompts/no_such_prompt_xyz")
        assert r.status_code == 404

    def test_put_prompt_then_get(self, admin_client: TestClient):
        # 写入新版本
        r = admin_client.put(
            "/v1/admin/prompts/semantic_extraction",
            json={
                "template": "v2 {summary} {entities}",
                "variables": ["summary", "entities"],
                "version": "2.0.0",
            },
        )
        assert r.status_code == 200
        version = r.json()["version"]
        assert version >= 1

        # 解析应返回新版
        r2 = admin_client.get("/v1/admin/prompts/semantic_extraction")
        assert r2.status_code == 200
        body = r2.json()
        assert body["template"] == "v2 {summary} {entities}"
        assert body["source"] == "global"

    def test_put_invalid_body_returns_400(self, admin_client: TestClient):
        r = admin_client.put(
            "/v1/admin/prompts/x",
            json={"variables": ["only_variables_no_template"]},
        )
        assert r.status_code == 400
        body = r.json()
        assert "/template" in body["detail"]["json_pointer"]

    def test_put_tenant_without_scope_id_returns_400(self, admin_client: TestClient):
        r = admin_client.put(
            "/v1/admin/prompts/x?scope=tenant",
            json={"template": "x"},
        )
        assert r.status_code == 400

    def test_put_with_variants(self, admin_client: TestClient):
        r = admin_client.put(
            "/v1/admin/prompts/sbd_llm_refine",
            json={
                "template": "Base {numbered_text}",
                "variables": ["numbered_text"],
                "variants": [
                    {
                        "match": {"tenant_id_in": ["beta_tenant"]},
                        "template": "Beta {numbered_text}",
                    }
                ],
            },
        )
        assert r.status_code == 200

        # 验证 variant 命中
        r2 = admin_client.get(
            "/v1/admin/prompts/sbd_llm_refine?tenant_id=beta_tenant"
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["template"] == "Beta {numbered_text}"
        assert body["source"] == "variant"

    def test_history_returns_writes(self, admin_client: TestClient):
        admin_client.put(
            "/v1/admin/prompts/semantic_extraction",
            json={"template": "h1 {summary} {entities}", "variables": ["summary", "entities"]},
        )
        admin_client.put(
            "/v1/admin/prompts/semantic_extraction",
            json={"template": "h2 {summary} {entities}", "variables": ["summary", "entities"]},
        )
        r = admin_client.get(
            "/v1/admin/prompts/semantic_extraction/history?limit=10"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] >= 2
        # 最新优先
        assert body["history"][0]["params"]["template"].startswith("h2")

    def test_render_basic(self, admin_client: TestClient):
        r = admin_client.post(
            "/v1/admin/prompts/episode_extraction/render",
            json={"variables": {"text": "我下周去北京"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert "我下周去北京" in body["rendered"]
        assert body["source"] in ("default", "builtin")

    def test_render_with_variant(self, admin_client: TestClient):
        r = admin_client.post(
            "/v1/admin/prompts/episode_extraction/render?tenant_id=acme",
            json={"variables": {"text": "x"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "variant"
        assert "Acme" in body["rendered"] or "Acme" in body["template"]

    def test_render_missing_var_returns_400(self, admin_client: TestClient):
        r = admin_client.post(
            "/v1/admin/prompts/episode_extraction/render",
            json={"variables": {}},
        )
        assert r.status_code == 400

    def test_render_unknown_returns_404(self, admin_client: TestClient):
        r = admin_client.post(
            "/v1/admin/prompts/no_such_prompt/render",
            json={"variables": {}},
        )
        assert r.status_code == 404

    def test_delete_marks_as_placeholder(self, admin_client: TestClient):
        # 先写一个
        admin_client.put(
            "/v1/admin/prompts/temp_prompt",
            json={"template": "temp {x}", "variables": ["x"]},
        )
        # 再删
        r = admin_client.delete("/v1/admin/prompts/temp_prompt")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

        # 解析仍返回但 template 是 placeholder
        r2 = admin_client.get("/v1/admin/prompts/temp_prompt")
        assert r2.status_code == 200
        assert r2.json()["template"] == "<<DELETED>>"
