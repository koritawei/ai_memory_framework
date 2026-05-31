"""Settings 验收：YAML 来源 + env 覆盖 + 缺失即 ValidationError。"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from pydantic import ValidationError

from memory_app.settings import (
    BOOTSTRAP_FILE_ENV,
    Settings,
    reset_settings_for_test,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project_cwd():
    """切到项目根，让 ``config/bootstrap.yaml`` 相对路径生效。"""
    cwd = os.getcwd()
    os.chdir(PROJECT_ROOT)
    try:
        yield PROJECT_ROOT
    finally:
        os.chdir(cwd)


def _write_bootstrap(path: Path, **overrides) -> None:
    """生成最小可用 bootstrap.yaml；overrides 用于改单字段。"""
    base = {
        "app_name": "Test Memory",
        "debug": False,
        "mongo_uri": "mongodb://test:27017",
        "mongo_db": "test_memory",
        "es_hosts": ["http://test:9200"],
        "es_index_prefix": "test_memory",
        "milvus_host": "test-milvus",
        "milvus_port": 19530,
        "milvus_collection": "test_vec",
        "redis_url": "redis://test:6379/0",
        "config_center_backend": "file",
        "config_center_file_path": "config/default.yaml",
        "auth_enabled": False,
        "admin_api_key": None,
        "api_key": None,
        "tenant_binding_enabled": False,
        "trust_gateway_headers": False,
        "jwt_secret": None,
        "jwt_algorithm": "HS256",
        "api_key_bindings": {},
        "dlq_backend": "memory",
        "task_runner_backend": "asyncio",
        "task_queue_key": "memory:tasks:cold_path",
        "task_runner_max_concurrent": 64,
        "cold_path_max_parallel": 64,
        "metrics_enabled": False,
        "rate_limit_enabled": False,
        "rate_limit_rpm": 120,
        "rate_limit_backend": "memory",
        "rate_limit_fail_open": True,
        "dlq_reconcile_interval_s": 0,
        "dlq_reconcile_batch_size": 100,
        "dlq_reconcile_max_retries": 5,
        "task_runner_consumer_enabled": True,
        "discover_entry_point_plugins": True,
        "plugin_entry_point_group": "memory_app.plugins",
        "strict_readiness": False,
    }
    base.update(overrides)
    path.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 仓库内 bootstrap.yaml 的"现实情况"测试
# ─────────────────────────────────────────────────────────────────────────────
def test_loads_from_shipped_bootstrap_yaml(project_cwd):
    """仓库内的 config/bootstrap.yaml 必须能被 Settings() 直接加载。"""
    reset_settings_for_test()
    s = Settings()
    # 这些值必须与 config/bootstrap.yaml 保持同步
    assert s.app_name == "Memory Service"
    assert s.mongo_db == "memory"
    assert s.config_center_backend == "file"
    assert s.config_center_file_path == "config/default.yaml"
    assert s.auth_enabled is False
    assert s.discover_entry_point_plugins is True


# ─────────────────────────────────────────────────────────────────────────────
# 自定义 YAML 路径
# ─────────────────────────────────────────────────────────────────────────────
def test_custom_yaml_via_env(tmp_path: Path, monkeypatch):
    custom = tmp_path / "boot_custom.yaml"
    _write_bootstrap(custom, app_name="Custom App", mongo_db="custom_db")
    monkeypatch.setenv(BOOTSTRAP_FILE_ENV, str(custom))
    reset_settings_for_test()
    s = Settings()
    assert s.app_name == "Custom App"
    assert s.mongo_db == "custom_db"


# ─────────────────────────────────────────────────────────────────────────────
# 优先级：env > YAML
# ─────────────────────────────────────────────────────────────────────────────
def test_env_overrides_yaml(tmp_path: Path, monkeypatch):
    yaml_path = tmp_path / "boot.yaml"
    _write_bootstrap(yaml_path, mongo_db="yaml_db", auth_enabled=False)
    monkeypatch.setenv(BOOTSTRAP_FILE_ENV, str(yaml_path))
    monkeypatch.setenv("MEMORY_MONGO_DB", "env_db")
    monkeypatch.setenv("MEMORY_AUTH_ENABLED", "true")
    reset_settings_for_test()
    s = Settings()
    assert s.mongo_db == "env_db"      # env 胜出
    assert s.auth_enabled is True


def test_yaml_provides_value_when_env_absent(tmp_path: Path, monkeypatch):
    yaml_path = tmp_path / "boot.yaml"
    _write_bootstrap(yaml_path, mongo_db="from_yaml_only")
    monkeypatch.setenv(BOOTSTRAP_FILE_ENV, str(yaml_path))
    monkeypatch.delenv("MEMORY_MONGO_DB", raising=False)
    reset_settings_for_test()
    s = Settings()
    assert s.mongo_db == "from_yaml_only"


# ─────────────────────────────────────────────────────────────────────────────
# 失效路径：YAML 缺失且 env 缺失 → ValidationError
# ─────────────────────────────────────────────────────────────────────────────
def test_yaml_missing_and_env_absent_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(BOOTSTRAP_FILE_ENV, str(tmp_path / "definitely_not_exist.yaml"))
    # 清空所有 MEMORY_* 环境变量
    for k in list(os.environ):
        if k.startswith("MEMORY_") and k != BOOTSTRAP_FILE_ENV:
            monkeypatch.delenv(k, raising=False)
    # 同时屏蔽 .env 文件影响
    monkeypatch.chdir(tmp_path)
    reset_settings_for_test()
    with pytest.raises(ValidationError):
        Settings()


def test_yaml_partial_env_补齐(tmp_path: Path, monkeypatch):
    """YAML 仅含部分字段、其余靠 env 补齐 → 应能加载成功。"""
    incomplete = tmp_path / "boot.yaml"
    incomplete.write_text(
        yaml.safe_dump({"app_name": "Partial", "debug": False}),
        encoding="utf-8",
    )
    monkeypatch.setenv(BOOTSTRAP_FILE_ENV, str(incomplete))
    # 通过 env 补齐其余必填字段
    env_kv = {
        "MONGO_URI": "mongodb://e:1",
        "MONGO_DB": "edb",
        "ES_HOSTS": '["http://e:9200"]',
        "ES_INDEX_PREFIX": "ep",
        "MILVUS_HOST": "h",
        "MILVUS_PORT": "1",
        "MILVUS_COLLECTION": "c",
        "REDIS_URL": "redis://e:6379/0",
        "CONFIG_CENTER_BACKEND": "file",
        "CONFIG_CENTER_FILE_PATH": "x.yaml",
        "AUTH_ENABLED": "false",
        "TENANT_BINDING_ENABLED": "false",
        "TRUST_GATEWAY_HEADERS": "false",
        "JWT_ALGORITHM": "HS256",
        "DLQ_BACKEND": "memory",
        "TASK_RUNNER_BACKEND": "asyncio",
        "TASK_QUEUE_KEY": "memory:tasks:cold_path",
        "TASK_RUNNER_MAX_CONCURRENT": "64",
        "COLD_PATH_MAX_PARALLEL": "64",
        "METRICS_ENABLED": "false",
        "RATE_LIMIT_ENABLED": "false",
        "RATE_LIMIT_RPM": "120",
        "RATE_LIMIT_BACKEND": "memory",
        "RATE_LIMIT_FAIL_OPEN": "true",
        "DLQ_RECONCILE_INTERVAL_S": "0",
        "DLQ_RECONCILE_BATCH_SIZE": "100",
        "DLQ_RECONCILE_MAX_RETRIES": "5",
        "TASK_RUNNER_CONSUMER_ENABLED": "true",
        "DISCOVER_ENTRY_POINT_PLUGINS": "true",
        "PLUGIN_ENTRY_POINT_GROUP": "memory_app.plugins",
        "STRICT_READINESS": "false",
    }
    for k, v in env_kv.items():
        monkeypatch.setenv(f"MEMORY_{k}", v)
    monkeypatch.chdir(tmp_path)  # 防止 .env 干扰
    reset_settings_for_test()
    s = Settings()
    assert s.app_name == "Partial"
    assert s.mongo_db == "edb"


# ─────────────────────────────────────────────────────────────────────────────
# 反向约束：运行时参数仍不能进入 Settings
# ─────────────────────────────────────────────────────────────────────────────
def test_runtime_params_still_not_in_settings(project_cwd):
    """运行时参数必须下沉到 ConfigCenter，不允许在 Settings 中。"""
    reset_settings_for_test()
    s = Settings()
    forbidden = [
        "embedding_model",
        "embedding_dim",
        "enable_graph",
        "enable_fisher_rao",
        "sbd_llm_fallback",
        "sync_write_timeout_ms",
    ]
    for name in forbidden:
        assert not hasattr(s, name), f"运行时参数 {name!r} 不应出现在 Settings"


def test_no_hardcoded_field_defaults():
    """除 admin_api_key 外，Settings 类不应有任何字段默认值（确保都来自 YAML/env）。"""
    fields = Settings.model_fields
    # 允许的"显式无值"字段
    allowed_optional = {"admin_api_key", "api_key", "jwt_secret", "api_key_bindings"}
    hardcoded = []
    for name, info in fields.items():
        if name in allowed_optional:
            continue
        # PydanticUndefined / required 字段没有默认值
        if info.is_required():
            continue
        hardcoded.append(name)
    assert not hardcoded, (
        f"以下字段在 settings.py 中存在硬编码默认值，违反「不在代码写死」要求：{hardcoded}"
    )
