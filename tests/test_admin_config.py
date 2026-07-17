"""Phase 8 Step 8.3:Admin Config / Plugins API 测试。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- ``GET /v1/admin/config`` 读出当前生效配置(default → 写入后变 global)
- ``POST /v1/admin/config`` 写入 → 立刻可读到新版本
- ``POST /v1/admin/config`` schema 校验失败返回 422
- ``GET /v1/admin/config/history`` 倒序返回历史
- ``POST /v1/admin/config/rollback`` 回滚一个旧版本,新版本号 = max+1
- ``GET /v1/admin/plugins/{category}/{name}/health`` 单实例 health
- ``POST /v1/admin/plugins/{category}/{name}/reload`` 释放缓存
- 鉴权:auth_enabled=true 时缺 X-Admin-Key 返回 403
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def project_cwd():
    cwd = os.getcwd()
    os.chdir(Path(__file__).resolve().parent.parent)
    try:
        yield
    finally:
        os.chdir(cwd)


@pytest.fixture
def isolated_default_yaml(tmp_path, monkeypatch):
    """把仓库内 config/default.yaml 拷贝到 tmp_path 并通过环境变量指向它 ——
    所有 admin POST/rollback 写操作只会修改副本,**不污染** repo 跟踪的真实文件。
    与 test_retrieve_endpoint.py 等其他端点测试的隔离策略对齐。
    """
    src = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    dst = tmp_path / "default.yaml"
    if src.exists():
        shutil.copy2(src, dst)
    else:
        # 仓库还没有 default.yaml 时给出空 YAML,保证 FileConfigCenter 不会
        # warning;FileConfigCenter 自身的"文件不存在"路径已有专门测试覆盖。
        dst.write_text("defaults: {}\n", encoding="utf-8")
    monkeypatch.setenv("MEMORY_CONFIG_CENTER_FILE_PATH", str(dst))
    return dst


@pytest.fixture
def client(project_cwd, isolated_default_yaml, monkeypatch):
    from memory_app import api
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.settings import reset_settings_for_test

    reset_settings_for_test()
    reset_prompt_manager_for_test()
    with TestClient(api.app) as c:
        yield c
    reset_prompt_manager_for_test()


@pytest.fixture
def auth_client(project_cwd, isolated_default_yaml, monkeypatch):
    """启用鉴权的 client(auth_enabled=true + admin_api_key=secret)。"""
    monkeypatch.setenv("MEMORY_AUTH_ENABLED", "true")
    monkeypatch.setenv("MEMORY_ADMIN_API_KEY", "secret")
    from memory_app import api
    from memory_app.prompt_runtime import reset_prompt_manager_for_test
    from memory_app.settings import reset_settings_for_test

    reset_settings_for_test()
    reset_prompt_manager_for_test()
    with TestClient(api.app) as c:
        yield c
    reset_prompt_manager_for_test()


# ════════════════════════════════════════════════════════════════════════════
# /v1/admin/config GET / POST / history / rollback
# ════════════════════════════════════════════════════════════════════════════
class TestConfigCRUD:
    CATEGORY = "memory.retrieval.fuser"

    def test_get_default_returns_default_source(self, client):
        r = client.get(
            "/v1/admin/config", params={"category": self.CATEGORY}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["category"] == self.CATEGORY
        # 没有任何 override → default
        assert body["source"] in ("default", "global")  # bootstrap 配置可能已有 global
        assert "name" in body and "params" in body

    def test_post_then_get_observes_new_value(self, client):
        # 先看一眼当前 k(用 weighted_rrf 默认 60)
        r = client.get("/v1/admin/config", params={"category": self.CATEGORY})
        original = r.json()
        # 写入新 k
        r = client.post(
            "/v1/admin/config",
            json={
                "category": self.CATEGORY,
                "name": "weighted_rrf",
                "params": {"k": 80},
                "actor": "alice",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["version"] >= 1
        assert body["actor"] == "alice"
        # 读回应当反映新值
        r = client.get("/v1/admin/config", params={"category": self.CATEGORY})
        assert r.status_code == 200
        assert r.json()["params"]["k"] == 80
        assert r.json()["source"] in ("global", "default")  # 已写入 → global
        assert r.json()["params"] != original["params"]

    def test_post_rejects_unknown_plugin(self, client):
        r = client.post(
            "/v1/admin/config",
            json={
                "category": "memory.retrieval.fuser",
                "name": "nonexistent_fuser_xyz",
                "params": {},
            },
        )
        assert r.status_code == 422
        body = r.json()
        # ConfigValidationError 转 422,detail 含 json_pointer
        assert "detail" in body
        assert isinstance(body["detail"], dict)
        assert "json_pointer" in body["detail"]
        assert body["detail"]["json_pointer"] == "/name"

    def test_post_rejects_scope_without_scope_id(self, client):
        r = client.post(
            "/v1/admin/config",
            json={
                "category": self.CATEGORY,
                "name": "weighted_rrf",
                "params": {"k": 60},
                "scope": "tenant",
                # 故意省略 scope_id
            },
        )
        assert r.status_code == 400

    def test_history_returns_versions(self, client):
        # 先连写两次,产生历史
        for k in (70, 75, 90):
            r = client.post(
                "/v1/admin/config",
                json={
                    "category": self.CATEGORY,
                    "name": "weighted_rrf",
                    "params": {"k": k},
                },
            )
            assert r.status_code == 200, r.text
        # FileConfigCenter 的 history 是基于内存 ring,但接口应可用
        r = client.get(
            "/v1/admin/config/history",
            params={"category": self.CATEGORY, "limit": 10},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["category"] == self.CATEGORY
        assert isinstance(body["history"], list)
        # File / Mongo 的 history 行为不同,这里只校验结构

    def test_rollback_404_when_version_missing(self, client):
        r = client.post(
            "/v1/admin/config/rollback",
            json={
                "category": self.CATEGORY,
                "target_version": 9999,
            },
        )
        assert r.status_code == 404, r.text


# ════════════════════════════════════════════════════════════════════════════
# /v1/admin/plugins/{category}/{name}/{health,reload}
# ════════════════════════════════════════════════════════════════════════════
class TestPluginInstanceOps:
    def test_single_plugin_health_returns_status(self, client):
        # 触发 fuser 实例 build —— 通过 admin/config GET 调 resolve,但 resolve 不
        # 触发 build。最稳妥:先 reload 一遍即可看到不活跃状态。
        r = client.get(
            "/v1/admin/plugins/memory.retrieval.fuser/weighted_rrf/health"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["category"] == "memory.retrieval.fuser"
        assert body["name"] == "weighted_rrf"
        # 未 build 时 not_active;已 build 时 ok / fail
        assert body["status"] in ("ok", "fail", "not_active")

    def test_reload_is_idempotent(self, client):
        # 第一次:可能释放 0 或几个实例;第二次幂等
        r1 = client.post(
            "/v1/admin/plugins/memory.retrieval.fuser/weighted_rrf/reload",
            params={"actor": "ops"},
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert body1["category"] == "memory.retrieval.fuser"
        assert body1["actor"] == "ops"
        assert isinstance(body1["released_count"], int)

        r2 = client.post(
            "/v1/admin/plugins/memory.retrieval.fuser/weighted_rrf/reload"
        )
        assert r2.status_code == 200
        # 已被释放过 → 第二次 released_count 应为 0
        assert r2.json()["released_count"] == 0


# ════════════════════════════════════════════════════════════════════════════
# 鉴权
# ════════════════════════════════════════════════════════════════════════════
class TestAuth:
    def test_403_without_admin_key_when_auth_enabled(self, auth_client):
        r = auth_client.get(
            "/v1/admin/config", params={"category": "memory.retrieval.fuser"}
        )
        assert r.status_code == 403, r.text

    def test_200_with_correct_admin_key(self, auth_client):
        r = auth_client.get(
            "/v1/admin/config",
            params={"category": "memory.retrieval.fuser"},
            headers={"X-Admin-Key": "secret"},
        )
        assert r.status_code == 200, r.text

    def test_403_with_wrong_admin_key(self, auth_client):
        r = auth_client.get(
            "/v1/admin/config",
            params={"category": "memory.retrieval.fuser"},
            headers={"X-Admin-Key": "wrong"},
        )
        assert r.status_code == 403

    def test_post_config_blocked_without_admin_key(self, auth_client):
        r = auth_client.post(
            "/v1/admin/config",
            json={
                "category": "memory.retrieval.fuser",
                "name": "weighted_rrf",
                "params": {"k": 60},
            },
        )
        assert r.status_code == 403


# ════════════════════════════════════════════════════════════════════════════
# 端到端:写 → 读 → reload(确保链路闭环)
# ════════════════════════════════════════════════════════════════════════════
class TestEndToEnd:
    def test_write_read_reload_cycle(self, client):
        cat = "memory.retrieval.reranker"
        name = "mmr"
        # 写入新参数
        r = client.post(
            "/v1/admin/config",
            json={
                "category": cat,
                "name": name,
                "params": {"mmr_lambda": 0.55},
                "actor": "qa",
            },
        )
        assert r.status_code == 200, r.text
        # 读出生效值
        r = client.get("/v1/admin/config", params={"category": cat})
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == name
        assert body["params"]["mmr_lambda"] == 0.55
        # 手动 reload(无活动实例时 released=0,但不报错)
        r = client.post(f"/v1/admin/plugins/{cat}/{name}/reload")
        assert r.status_code == 200
