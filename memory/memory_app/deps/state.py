"""AppState —— 全局应用状态容器(原 ``deps.py`` 拆解后的精简版)。

═══════════════════════════════════════════════════════════════════════════════
本类只承担三件事
═══════════════════════════════════════════════════════════════════════════════
1. **字段容器**:外部客户端(委托 :class:`ExternalClients`)+ 横切组件
   (ConfigCenter / PluginFactory) + 各业务服务
2. **生命周期编排**:
   - :meth:`init`  → ExternalClients.init → ConfigCenter/PluginFactory →
     按顺序遍历 :data:`BUILDERS` → 各 builder.build
   - :meth:`close` → BackgroundTaskRunner → PluginFactory → ConfigCenter →
     ExternalClients
3. **健康聚合委托**::meth:`healthchecks` → :class:`HealthAggregator`

历史上单文件 ``deps.py`` 1061 行的"上帝类"已拆为:
- ``deps/clients.py``     外部客户端组
- ``deps/health.py``      健康聚合
- ``deps/builders/*.py``  6 个业务 builder（每个 ≤120 行）
- ``deps/depends.py``     FastAPI Depends 工厂

本文件应保持 ≤200 行。新增业务能力时**只**追加 builder，不改本文件。
"""

from __future__ import annotations

import logging

from memory_app.config_center import ConfigCenter, FileConfigCenter
from memory_app.deps.builders import BUILDERS
from memory_app.deps.clients import ExternalClients
from memory_app.deps.health import HealthAggregator
from memory_app.plugins import PluginFactory, registry as plugin_registry
from memory_app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class AppState:
    """全局应用状态容器(精简版)。

    属性约定:
    - :attr:`clients` 始终非 None;其内部子客户端按可达性 init
    - :attr:`config_center` 与 :attr:`plugin_factory` 是必启项 —— init 失败抛
    - 各业务服务字段在对应 builder 装配失败时保持 None，Depends 工厂
      据此抛 503
    """

    def __init__(self) -> None:
        self.settings: Settings | None = None

        # 外部客户端组(可降级)
        self.clients = ExternalClients()

        # 横切组件(必启项)
        self.config_center: ConfigCenter | None = None
        self.plugin_factory: PluginFactory | None = None

        # 业务服务字段(由各 ServiceBuilder 装配)
        self.ingest_service = None
        self.cold_path_service = None
        self.background_runner = None
        self.retrieval_orchestrator = None
        self.feedback_service = None
        self.lifecycle_updater = None
        self.importance_scorer = None
        self.consolidation_service = None
        self.consolidator = None
        self.capacity_optimizer = None
        self.entity_store = None
        self.entity_extractor = None
        self.memory_graph = None
        self.mongo_repo = None
        self.dlq = None

    # ════════════════════════════════════════════════════════════════════════
    # 向后兼容只读访问器(旧代码 / 测试 / health 路由仍可用)
    # ════════════════════════════════════════════════════════════════════════
    @property
    def mongo_client(self):
        return self.clients.mongo_client

    @property
    def mongo_db(self):
        return self.clients.mongo_db

    @property
    def es_client(self):
        return self.clients.es_client

    @property
    def redis_client(self):
        return self.clients.redis_client

    @property
    def milvus_alias(self) -> str | None:
        return self.clients.milvus_alias

    @property
    def _milvus_connected(self) -> bool:
        return self.clients.milvus_connected

    # ════════════════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════════════════
    async def init(self, settings: Settings | None = None) -> None:
        """启动期初始化：外部客户端 → 配置中心 → 插件工厂 → 各业务 builder。"""
        self.settings = settings or get_settings()

        # 1. ConfigCenter —— 必启项
        if self.settings.config_center_backend == "file":
            self.config_center = FileConfigCenter(self.settings.config_center_file_path)
        elif self.settings.config_center_backend == "mongo":
            from memory_app.config_center.mongo_center import MongoConfigCenter

            # MongoConfigCenter 必须先有 mongo_client
            await self.clients.init_mongo(self.settings)
            self.config_center = MongoConfigCenter(
                self.clients.mongo_client, self.settings.mongo_db
            )
        else:
            # 不可能进入 —— Settings Literal["file", "mongo"] 已限制
            raise ValueError(
                f"unknown config_center_backend: {self.settings.config_center_backend}"
            )

        # 2. PluginFactory + ConfigCenter watcher
        self.plugin_factory = PluginFactory(plugin_registry, self.config_center)
        await self.plugin_factory.attach_config_center(self.config_center)

        # 3. 触发默认插件注册(plugins_default/__init__.py 内 @register)
        try:
            import memory_app.plugins_default  # noqa: F401
        except Exception as e:  # noqa: BLE001
            logger.warning("loading plugins_default failed: %s", e)

        # 4. 第三方 entry-point 插件
        if self.settings.discover_entry_point_plugins:
            try:
                count = plugin_registry.discover_entry_points(
                    self.settings.plugin_entry_point_group
                )
                if count:
                    logger.info("discovered %d third-party plugins", count)
            except Exception as e:  # noqa: BLE001
                logger.warning("entry point discovery failed: %s", e)

        # 5. 外部客户端组(lazy & non-blocking)
        await self.clients.init(self.settings)

        # 6. 按顺序遍历 BUILDERS —— 任一 builder 失败仅 warn
        for builder in BUILDERS:
            if not builder.can_build(self):
                logger.info(
                    "skip builder %s (requires not ready: %s)",
                    builder.name, builder.requires,
                )
                continue
            try:
                await builder.build(self)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "builder %s failed (degraded): %s", builder.name, e
                )

    async def close(self) -> None:
        """优雅关闭。各子项独立 try/except。"""
        if self.background_runner is not None:
            try:
                await self.background_runner.shutdown(timeout_s=5.0)
            except Exception as e:  # noqa: BLE001
                logger.warning("background runner shutdown failed: %s", e)
        if self.plugin_factory is not None:
            try:
                await self.plugin_factory.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.warning("plugin factory shutdown failed: %s", e)
        if self.config_center is not None:
            try:
                await self.config_center.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("config center close failed: %s", e)
        await self.clients.close()

    # ════════════════════════════════════════════════════════════════════════
    # 健康检查
    # ════════════════════════════════════════════════════════════════════════
    async def healthchecks(self) -> dict[str, dict]:
        """委托 :class:`HealthAggregator`。"""
        return await HealthAggregator(self).check()


#: 模块级单例(FastAPI 单进程足够;多进程部署各副本各自一份)
app_state = AppState()


__all__ = ["AppState", "app_state"]
