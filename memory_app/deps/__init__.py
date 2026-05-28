"""``memory_app.deps`` —— 应用全局态 + 装配 + Depends 工厂包入口。

═══════════════════════════════════════════════════════════════════════════════
背向兼容
═══════════════════════════════════════════════════════════════════════════════
原 ``memory_app/deps.py`` 单文件(1061 行)已拆为本包:

- :class:`AppState`               全局状态容器(精简版,见 :mod:`.state`)
- :data:`app_state`               模块级单例
- :class:`ExternalClients`        外部客户端组(见 :mod:`.clients`)
- :class:`HealthAggregator`       健康聚合器(见 :mod:`.health`)
- :data:`BUILDERS`                各业务 ServiceBuilder 注册表（见 :mod:`.builders`）
- ``get_*``                       FastAPI Depends 工厂(见 :mod:`.depends`)

公共 import 路径保持不变:

.. code-block:: python

    from memory_app.deps import app_state, AppState
    from memory_app.deps import get_ingest_service, get_retrieval_orchestrator, ...
"""

from __future__ import annotations

from memory_app.deps.builders import BUILDERS, ServiceBuilder
from memory_app.deps.clients import ExternalClients
from memory_app.deps.depends import (
    get_consolidation_service,
    get_entity_store,
    get_feedback_service,
    get_ingest_service,
    get_memory_graph,
    get_mongo_repo,
    get_retrieval_orchestrator,
)
from memory_app.deps.health import HealthAggregator
from memory_app.deps.state import AppState, app_state

__all__ = [
    # 核心
    "AppState",
    "app_state",
    "ExternalClients",
    "HealthAggregator",
    "ServiceBuilder",
    "BUILDERS",
    # Depends
    "get_ingest_service",
    "get_retrieval_orchestrator",
    "get_feedback_service",
    "get_consolidation_service",
    "get_memory_graph",
    "get_mongo_repo",
    "get_entity_store",
]
