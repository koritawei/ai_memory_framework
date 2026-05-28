"""DBConfigCenter —— 关系/文档型 DB 后端的共享骨架。

继承 :class:`BaseConfigCenter`，把 4 个生命周期 hook 用 9 个细粒度 DB CRUD
原语实现：

CRUD 原语（DB 子类必须实现）：
    - :meth:`_db_ensure_schema`              建表 / 建索引
    - :meth:`_db_find_entry`                 按 (category, scope, scope_id) 查 entry
    - :meth:`_db_upsert_entry`               写 entry（含 version 字段）
    - :meth:`_db_query_overrides_by_scope`   按 scope 批量拉所有 overrides
    - :meth:`_db_insert_history`             写一条 history
    - :meth:`_db_query_history`              按 category 倒序查 history
    - :meth:`_db_start_native_watch`         开启原生 watch（Mongo Change Stream / PG LISTEN）
    - :meth:`_db_stop_native_watch`          停止 watch
    - :meth:`_db_ping`                       健康检查

通用流程（DBConfigCenter 实现）：
    - ``_load_overrides`` ：3 次 ``_db_query_overrides_by_scope`` 拼装
    - ``_persist_entry``  ：``_db_find_entry`` → version+1 → ``_db_upsert_entry`` → ``_db_insert_history``
    - ``_read_history``   ：直接转 ``_db_query_history``
    - ``_spawn_watcher``  ：``_db_ensure_schema`` + ``_db_start_native_watch``
    - ``_stop_watcher``   ：``_db_stop_native_watch``

为了避免高 QPS 下每次 resolve 都打 3 次 DB，本类内置 TTL 缓存
(:attr:`overrides_cache_ttl_seconds`，默认 5 秒)；watcher 触发时主动失效。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from .base import ConfigChangeEvent
from ._common import BaseConfigCenter

logger = logging.getLogger(__name__)


class DBConfigCenter(BaseConfigCenter):
    """适用任意关系/文档型 DB 的共享 ConfigCenter 骨架。"""

    #: overrides 缓存 TTL；watcher 收到事件会主动失效，TTL 仅作兜底
    overrides_cache_ttl_seconds: float = 5.0

    def __init__(self, *, defaults_flat: dict | None = None, resolver=None) -> None:
        super().__init__(defaults_flat=defaults_flat, resolver=resolver)
        self._overrides_cache: tuple[dict, dict, dict] | None = None
        self._overrides_cache_ts: float = 0.0
        self._cache_lock = asyncio.Lock()

    # ════════════════════════════════════════════════════════════
    # 子类必须实现的 9 个 DB CRUD 原语
    # ════════════════════════════════════════════════════════════
    @abstractmethod
    async def _db_ensure_schema(self) -> None:
        """建表 / 建索引（幂等）。"""

    @abstractmethod
    async def _db_find_entry(
        self, *, category: str, scope: str, scope_id: Optional[str]
    ) -> dict | None:
        """按主键查 entry。返回完整 doc（含 version）或 None。"""

    @abstractmethod
    async def _db_upsert_entry(self, doc: dict) -> None:
        """upsert entry。doc 已含 ``category / scope / scope_id / name / params /
        version / updated_at / actor``。"""

    @abstractmethod
    async def _db_query_overrides_by_scope(self, scope: str) -> dict:
        """按 scope 批量拉取，返回符合 :meth:`BaseConfigCenter._load_overrides` 约定的结构。

        - scope=global → ``{category: entry}``
        - scope=tenant → ``{tenant_id: {category: entry}}``
        - scope=user   → ``{user_id: {category: entry}}``
        """

    @abstractmethod
    async def _db_insert_history(self, doc: dict) -> None:
        """写一条历史。"""

    @abstractmethod
    async def _db_query_history(self, category: str, limit: int) -> list[dict]:
        """按 category 倒序查历史。"""

    @abstractmethod
    async def _db_start_native_watch(
        self, on_event: Callable[[ConfigChangeEvent], Awaitable[None]]
    ) -> None:
        """启动后端 native watch。子类应在变更发生时回调 ``on_event``，并主动调用
        :meth:`invalidate_cache` 让 resolve 立即看到新值。"""

    @abstractmethod
    async def _db_stop_native_watch(self) -> None:
        """停止 native watch。"""

    @abstractmethod
    async def _db_ping(self) -> bool:
        """连通性探测。"""

    # ════════════════════════════════════════════════════════════
    # BaseConfigCenter 4 hooks 的 DB 实现
    # ════════════════════════════════════════════════════════════
    async def _load_overrides(self) -> tuple[dict, dict, dict]:
        async with self._cache_lock:
            now = time.monotonic()
            if (
                self._overrides_cache is not None
                and now - self._overrides_cache_ts < self.overrides_cache_ttl_seconds
            ):
                return self._overrides_cache
            g = await self._db_query_overrides_by_scope("global")
            t = await self._db_query_overrides_by_scope("tenant")
            u = await self._db_query_overrides_by_scope("user")
            self._overrides_cache = (g, t, u)
            self._overrides_cache_ts = now
            return self._overrides_cache

    async def _persist_entry(
        self,
        *,
        category: str,
        scope: str,
        scope_id: Optional[str],
        entry: dict,
        actor: str,
    ) -> int:
        prev = await self._db_find_entry(category=category, scope=scope, scope_id=scope_id)
        new_version = (prev.get("version", 0) if prev else 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "category": category,
            "scope": scope,
            "scope_id": scope_id,
            "name": entry["name"],
            "params": entry.get("params", {}) or {},
            "variants": entry.get("variants"),
            "version": new_version,
            "updated_at": now,
            "actor": actor,
        }
        await self._db_upsert_entry(doc)
        if prev:
            archived = dict(prev)
            archived.pop("_id", None)
            archived["archived_at"] = now
            try:
                await self._db_insert_history(archived)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "failed to insert history for %s/%s/%s: %s", category, scope, scope_id, e
                )
        # 写后立即失效缓存，下次 resolve 拉新
        self.invalidate_cache()
        return new_version

    async def _read_history(self, category: str, limit: int) -> list[dict]:
        return await self._db_query_history(category, limit)

    async def _spawn_watcher(
        self, on_native_event: Callable[[ConfigChangeEvent], Awaitable[None]]
    ) -> None:
        await self._db_ensure_schema()

        async def _wrapped(event: ConfigChangeEvent) -> None:
            self.invalidate_cache()
            self._bump_version()
            await on_native_event(event)

        await self._db_start_native_watch(_wrapped)

    async def _stop_watcher(self) -> None:
        await self._db_stop_native_watch()

    # ════════════════════════════════════════════════════════════
    # 公共便利
    # ════════════════════════════════════════════════════════════
    def invalidate_cache(self) -> None:
        """主动让 overrides 缓存失效，下次 resolve 重新拉 DB。"""
        self._overrides_cache = None
        self._overrides_cache_ts = 0.0

    async def health(self) -> dict:
        try:
            ok = await self._db_ping()
        except Exception as e:  # noqa: BLE001
            return {"status": "fail", "detail": str(e)}
        return {"status": "ok"} if ok else {"status": "fail", "detail": "ping returned False"}


__all__ = ["DBConfigCenter"]
