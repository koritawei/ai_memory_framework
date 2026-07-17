"""HealthAggregator —— /health/ready 的状态聚合(从原 ``AppState.healthchecks`` 抽出)。

═══════════════════════════════════════════════════════════════════════════════
聚合策略
═══════════════════════════════════════════════════════════════════════════════
- 各子项独立 try/except —— 单组件 ping 失败不影响其他组件返回 ok
- 必启项(ConfigCenter / PluginRegistry) status=fail 时,即便 strict=False
  也应被 health 路由判定为 503
- 可降级项(Mongo / ES / Redis / Milvus) status=fail 时:
  - ``strict_readiness=true``  → /health/ready 返回 503
  - 否则                       → 返回 200 + degraded
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from memory_app.plugins import registry as plugin_registry

if TYPE_CHECKING:
    from memory_app.deps.state import AppState

logger = logging.getLogger(__name__)


class HealthAggregator:
    """聚合 AppState 中所有可达组件的健康状态。"""

    def __init__(self, state: "AppState") -> None:
        self._state = state

    async def check(self) -> dict[str, dict]:
        """返回 ``{component_name: {"status": "ok|fail|degraded", "detail": "..."}}``。"""
        out: dict[str, dict] = {}
        out["mongo"] = await self._check_mongo()
        out["es"] = await self._check_es()
        out["redis"] = await self._check_redis()
        out["milvus"] = self._check_milvus()
        out["config_center"] = await self._check_config_center()
        out["plugin_registry"] = {
            "status": "ok",
            "detail": f"categories={len(plugin_registry.categories())}",
        }
        out["core_services"] = self._check_core_services()
        return out

    def _check_core_services(self) -> dict:
        """核心业务能力是否装配（ingest / retrieval）。"""
        missing: list[str] = []
        if self._state.ingest_service is None:
            missing.append("ingest_service")
        if self._state.retrieval_orchestrator is None:
            missing.append("retrieval_orchestrator")
        if missing:
            return {
                "status": "fail",
                "detail": f"missing: {', '.join(missing)}",
            }
        return {"status": "ok", "detail": "ingest+retrieval ready"}

    # ────────────────────────────────────────────────────────────────────────
    # 子项
    # ────────────────────────────────────────────────────────────────────────
    async def _check_mongo(self) -> dict:
        db = self._state.clients.mongo_db
        if db is None:
            return {"status": "fail", "detail": "client not initialized"}
        try:
            await db.command("ping")
            return {"status": "ok"}
        except Exception as e:  # noqa: BLE001
            return {"status": "fail", "detail": str(e)}

    async def _check_es(self) -> dict:
        client = self._state.clients.es_client
        if client is None:
            return {"status": "fail", "detail": "client not initialized"}
        try:
            await client.ping()
            return {"status": "ok"}
        except Exception as e:  # noqa: BLE001
            return {"status": "fail", "detail": str(e)}

    async def _check_redis(self) -> dict:
        client = self._state.clients.redis_client
        if client is None:
            return {"status": "fail", "detail": "client not initialized"}
        try:
            await client.ping()
            return {"status": "ok"}
        except Exception as e:  # noqa: BLE001
            return {"status": "fail", "detail": str(e)}

    def _check_milvus(self) -> dict:
        clients = self._state.clients
        if clients.milvus_connected:
            return {"status": "ok", "detail": f"alias={clients.milvus_alias}"}
        return {"status": "fail", "detail": "not connected"}

    async def _check_config_center(self) -> dict:
        cc = self._state.config_center
        if cc is None:
            return {"status": "fail"}
        return await cc.health()


__all__ = ["HealthAggregator"]
