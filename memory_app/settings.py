"""启动期不可变配置（pydantic-settings v2，对齐设计文档 §2.8.6.2）。

═══════════════════════════════════════════════════════════════════════════════
Settings 与 ConfigCenter 的边界
═══════════════════════════════════════════════════════════════════════════════
本模块**只**承载启动期不可变的引导性配置：

- DB / Cache / 搜索引擎 URI（运行中切换需重启进程）
- ConfigCenter 后端类型与文件路径（决定运行时配置中心怎么拉起来）
- 鉴权总开关与管理员 API Key（影响所有路由的访问控制）
- 启动期行为：是否扫描第三方 entry-point 插件、健康检查严格度

所有运行时可调参数（阈值 / 权重 / 模型名 / cron 等）走 :mod:`memory_app.config_center`，
不在此声明。这与 §2.8.1「单一事实源」原则严格对齐 —— 避免「环境变量与配置中心
两套真值源」的歧义。

═══════════════════════════════════════════════════════════════════════════════
数据来源优先级（高 → 低）
═══════════════════════════════════════════════════════════════════════════════
1. ``Settings(...)`` 显式 init kwargs（仅测试场景使用）
2. 环境变量（前缀 ``MEMORY_``）—— K8s Secret / Vault / CI 注入路径
3. YAML 文件：默认 ``config/bootstrap.yaml``，可经 ``MEMORY_BOOTSTRAP_FILE`` 重定向
4. ``.env`` 文件（dotenv，本地开发便利）
5. 文件 secrets

═══════════════════════════════════════════════════════════════════════════════
关键约束（被测试反向校验）
═══════════════════════════════════════════════════════════════════════════════
- **Settings 字段在 Python 代码中禁止写硬编码默认值**（仅 ``Optional[str] = None``
  例外，因为语义为「无值」而非配置）。缺失即 ``ValidationError``，强制运维通过
  YAML 或 env 显式提供，避免「忘了配置导致连本地 dev DB」的事故。
- 该约束由 ``tests/test_settings.py::test_no_hardcoded_field_defaults`` 在 CI 中守护。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal, Tuple, Type

import yaml
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# YAML bootstrap 来源
# ─────────────────────────────────────────────────────────────────────────────
#: 默认 bootstrap YAML 路径（相对工作目录）
DEFAULT_BOOTSTRAP_FILE = "config/bootstrap.yaml"

#: 用户可通过该环境变量重定向到任意路径
#: 典型用法：
#:   MEMORY_BOOTSTRAP_FILE=/etc/memory/bootstrap.prod.yaml uvicorn ...
BOOTSTRAP_FILE_ENV = "MEMORY_BOOTSTRAP_FILE"


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """从 YAML 读取 Settings 字段值的自定义 pydantic-settings source。

    与官方 ``YamlConfigSettingsSource`` 的差异：
    - 文件缺失时**仅 warn 不抛错**（让 env 仍有机会补齐）
    - 单实例内自带缓存，避免每个字段查询都重新读盘
    - 解析失败时降级为空 dict 并 error log，不让单个 YAML 错误阻断整个启动
    """

    def __init__(self, settings_cls, yaml_path: str | os.PathLike[str]):
        super().__init__(settings_cls)
        self._yaml_path = Path(yaml_path)
        self._cache: dict[str, Any] | None = None
        # 第一次发现文件缺失时打 warn，后续多次读取保持静默
        self._warned_missing = False

    def _load(self) -> dict[str, Any]:
        """惰性加载 YAML 全文；多次调用复用同一结果。"""
        if self._cache is not None:
            return self._cache
        if not self._yaml_path.exists():
            if not self._warned_missing:
                logger.warning(
                    "bootstrap settings file %s not found; falling back to env vars only",
                    self._yaml_path,
                )
                self._warned_missing = True
            self._cache = {}
            return self._cache
        try:
            with self._yaml_path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                # YAML 顶层必须是 mapping；若是 list / scalar 视为配置错误
                logger.error(
                    "bootstrap settings file %s is not a YAML mapping; ignored",
                    self._yaml_path,
                )
                self._cache = {}
            else:
                self._cache = data
        except Exception as e:  # noqa: BLE001 —— 任何解析错都降级，不阻断启动
            logger.error("failed to load bootstrap settings %s: %s", self._yaml_path, e)
            self._cache = {}
        return self._cache

    def get_field_value(self, field, field_name: str):  # type: ignore[override]
        """单字段读取入口（pydantic-settings 内部按需调用）。"""
        return self._load().get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        """整体读取入口（pydantic-settings 拼装最终 init kwargs 时调用）。"""
        return self._load()


def _resolve_bootstrap_path() -> str:
    """决定本次进程要从哪个 YAML 文件加载 Settings。"""
    return os.environ.get(BOOTSTRAP_FILE_ENV, DEFAULT_BOOTSTRAP_FILE)


# ─────────────────────────────────────────────────────────────────────────────
# Settings —— 启动期不可变配置（无任何代码内置默认值）
# ─────────────────────────────────────────────────────────────────────────────
class Settings(BaseSettings):
    """启动期不可变配置。

    所有字段（除 ``admin_api_key``）都没有 Python 默认值；缺失即 ``ValidationError``，
    强制运维通过 YAML 或 env 显式声明。这是 §2.8.6.2 中的关键设计决策。
    """

    # ── 服务元信息 ──
    app_name: str               # 暴露在 OpenAPI ``info.title``，便于多实例区分
    debug: bool                 # 本地调试开关；生产应为 false

    # ── MongoDB ──（启动期连接，URI 不可热改）
    mongo_uri: str
    mongo_db: str

    # ── Elasticsearch ──
    es_hosts: list[str]
    es_index_prefix: str

    # ── Milvus ──
    milvus_host: str
    milvus_port: int
    milvus_collection: str

    # ── Redis ──（用于 SBD 状态、幂等键、缓存等横切场景）
    redis_url: str

    # ── 配置中心后端选择（启动期决定，运行中不可改）──
    config_center_backend: Literal["file", "mongo"]
    config_center_file_path: str   # 仅 file 后端使用；mongo 后端忽略

    # ── 鉴权 ──
    auth_enabled: bool
    #: 唯一允许 Optional 的字段：null = 未配置，配置后启用 X-Admin-Key 校验。
    #: 该字段用 None 而非空字符串便于明确区分「未配置」与「显式配置为空」。
    admin_api_key: str | None = None
    #: 业务 API Bearer 令牌；``auth_enabled=true`` 且非 null 时，
    #: ``/v1/memory/*`` / ``/v1/query/*`` 要求 ``Authorization: Bearer``。
    api_key: str | None = None
    #: true 时请求体 ``tenant_id`` / ``user_id`` 必须与 API Key 绑定或 JWT claim 一致
    tenant_binding_enabled: bool
    trust_gateway_headers: bool
    jwt_secret: str | None = None
    jwt_algorithm: str
    #: ``{api_key: {tenant_id, user_id?}}`` —— 静态密钥到租户映射
    api_key_bindings: dict[str, dict[str, str]] | None = None

    # ── DLQ / 后台任务 ──
    dlq_backend: Literal["memory", "mongo", "redis"]
    task_runner_backend: Literal["asyncio", "redis"]
    task_queue_key: str

    # ── 可观测 / 限流 ──
    metrics_enabled: bool
    rate_limit_enabled: bool
    rate_limit_rpm: int
    rate_limit_backend: Literal["memory", "redis"]

    # ── DLQ Reconciler ──
    dlq_reconcile_interval_s: int
    dlq_reconcile_batch_size: int
    dlq_reconcile_max_retries: int
    task_runner_consumer_enabled: bool

    # ── 并发预算（防止无界 fan-out）──
    background_max_concurrent: int
    sync_index_max_concurrent: int
    cold_path_llm_max_concurrent: int

    # ── 启动行为 ──
    #: 是否扫描第三方包（``[project.entry-points."memory_app.plugins"]``）注册的插件
    discover_entry_point_plugins: bool
    plugin_entry_point_group: str

    # ── 健康检查行为 ──
    #: True 时，外部依赖任一失败即 /health/ready 返回 fail；
    #: 默认 False，开发态下 mongo/redis/milvus 缺失只显示 degraded，不影响 K8s 探活
    strict_readiness: bool

    model_config = SettingsConfigDict(
        env_prefix="MEMORY_",          # 所有 env 变量必须 ``MEMORY_`` 前缀
        env_file=".env",               # 本地开发支持 .env 文件
        env_file_encoding="utf-8",
        extra="ignore",                # YAML 中多余字段忽略（兼容未来扩展）
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        """定义来源优先级链。

        pydantic-settings 按 tuple 顺序合并各来源，**前面的来源覆盖后面的**。
        即：``init > env > YAML > .env > secrets``。
        """
        yaml_source = _YamlSettingsSource(settings_cls, _resolve_bootstrap_path())
        return (
            init_settings,
            env_settings,
            yaml_source,
            dotenv_settings,
            file_secret_settings,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 单例访问器
# ─────────────────────────────────────────────────────────────────────────────
# 模块级单例：避免每次 ``get_settings()`` 都重新解析 YAML / env
_settings: Settings | None = None


def get_settings() -> Settings:
    """单例 Settings 访问器。

    生产代码应**只**通过本函数取 Settings；测试若需修改 env 或 YAML，先调用
    :func:`reset_settings_for_test` 清空缓存。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_test() -> None:
    """重置 Settings 单例。仅供测试使用。

    典型场景：测试中通过 ``monkeypatch.setenv(...)`` / 切换 ``MEMORY_BOOTSTRAP_FILE``
    后，必须先调用本函数让下次 :func:`get_settings` 重新读取。
    """
    global _settings
    _settings = None


__all__ = [
    "Settings",
    "get_settings",
    "reset_settings_for_test",
    "BOOTSTRAP_FILE_ENV",
    "DEFAULT_BOOTSTRAP_FILE",
]
