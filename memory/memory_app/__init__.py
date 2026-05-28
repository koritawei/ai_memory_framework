"""Memory Service —— AI 分层认知记忆系统。

整个包按"业务平面（在线写入/检索/离线认知）+ 横切平面（插件 SPI / 配置中心）"组织：

- ``memory_app.plugins``         插件 SPI 抽象 + 注册表 + 工厂
- ``memory_app.plugins_default`` 项目内置默认插件实现
- ``memory_app.config_center``   配置中心（A+B 嵌套类层级，）
- ``memory_app.routers``         FastAPI 路由（健康检查、Admin API、未来 ingest/retrieve...）
- ``memory_app.settings``        启动期不可变配置（从 ``config/bootstrap.yaml`` 加载，）
- ``memory_app.deps``            外部依赖连接池与全局状态容器
- ``memory_app.api``             FastAPI 应用入口


"""

# 语义化版本，对外通过 OpenAPI ``info.version`` 暴露
__version__ = "0.1.0"
