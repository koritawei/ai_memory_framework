"""MongoConfigCenter —— 生产 ConfigCenter（基于 MongoDB）。

继承 :class:`DBConfigCenter`，只实现 9 个 ``_db_*`` CRUD 原语；
通用流程（resolve / write / history / watch / 缓存 / version 自增）由
:class:`DBConfigCenter` + :class:`BaseConfigCenter` 完成。

Phase 8 Step 8.2:``_db_start_native_watch`` 落地 MongoDB Change Stream:
- 启动后台 task 循环 ``collection.watch(...)`` 拉变更
- 把 insert / update / replace 转成 :class:`ConfigChangeEvent` 喂给 ``on_event``
- 网络抖断后指数退避重连(1s → 30s 上限)
- ``_db_stop_native_watch`` 触发 ``stop_event`` + 取消 task 干净退出

注意:Change Stream **要求** Mongo 服务端是副本集(replica set)或分片集群
单节点 standalone 不支持。本实现优雅降级:``watch()`` 抛错时仅 log + 退避重试,
TTL 缓存(默认 5s)兜底,业务平面不受影响。

依赖:``motor.motor_asyncio.AsyncIOMotorClient`` 由调用方注入。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from .base import ConfigChangeEvent
from ._db import DBConfigCenter

logger = logging.getLogger(__name__)


# 文档默认 collection 名（可通过构造函数覆盖）
DEFAULT_ENTRY_COLLECTION = "global_config"
DEFAULT_HISTORY_COLLECTION = "global_config_history"

#: Change Stream 重连退避上限(秒);单次失败后从 1s 翻倍逼近
WATCH_BACKOFF_MAX_SECONDS: float = 30.0
#: Change Stream 起始退避(秒)
WATCH_BACKOFF_INITIAL_SECONDS: float = 1.0


class MongoConfigCenter(DBConfigCenter):
    """MongoDB 后端 ConfigCenter。

    :param mongo_client: ``AsyncIOMotorClient`` 实例
    :param db_name: MongoDB 数据库名
    :param entry_collection: 配置 entry collection 名
    :param history_collection: 历史 collection 名
    :param defaults_flat: 默认 defaults（可由调用方在初始化后注入或不提供）
    """

    def __init__(
        self,
        mongo_client,  # AsyncIOMotorClient（避免在 import 时硬依赖 motor）
        db_name: str = "memory",
        *,
        entry_collection: str = DEFAULT_ENTRY_COLLECTION,
        history_collection: str = DEFAULT_HISTORY_COLLECTION,
        defaults_flat: dict | None = None,
    ) -> None:
        super().__init__(defaults_flat=defaults_flat)
        self._client = mongo_client
        self._db = mongo_client[db_name]
        self._coll = self._db[entry_collection]
        self._hist = self._db[history_collection]
        # Change Stream 状态
        self._native_watch_started = False
        self._watch_task: asyncio.Task | None = None
        self._watch_stop: asyncio.Event | None = None

    # ════════════════════════════════════════════════════════════
    # 9 个 DB CRUD 原语 —— motor 适配
    # ════════════════════════════════════════════════════════════
    async def _db_ensure_schema(self) -> None:
        # 主键唯一索引：(category, scope, scope_id)
        await self._coll.create_index(
            [("category", 1), ("scope", 1), ("scope_id", 1)],
            unique=True,
            name="uniq_category_scope",
        )
        # 历史按 category + version 倒序查
        await self._hist.create_index(
            [("category", 1), ("version", -1)], name="hist_category_version"
        )

    async def _db_find_entry(
        self, *, category: str, scope: str, scope_id: Optional[str]
    ) -> dict | None:
        return await self._coll.find_one(
            {"category": category, "scope": scope, "scope_id": scope_id}
        )

    async def _db_upsert_entry(self, doc: dict) -> None:
        await self._coll.update_one(
            {
                "category": doc["category"],
                "scope": doc["scope"],
                "scope_id": doc["scope_id"],
            },
            {"$set": doc},
            upsert=True,
        )

    async def _db_query_overrides_by_scope(self, scope: str) -> dict:
        cursor = self._coll.find({"scope": scope})
        out: dict = {}
        async for d in cursor:
            entry = {
                "name": d["name"],
                "params": d.get("params", {}) or {},
            }
            if d.get("variants"):
                entry["variants"] = d["variants"]
            if scope == "global":
                out[d["category"]] = entry
            else:
                sid = d.get("scope_id")
                if sid is None:
                    continue
                out.setdefault(sid, {})[d["category"]] = entry
        return out

    async def _db_insert_history(self, doc: dict) -> None:
        await self._hist.insert_one(doc)

    async def _db_query_history(self, category: str, limit: int) -> list[dict]:
        cursor = self._hist.find({"category": category}).sort("version", -1).limit(limit)
        out: list[dict] = []
        async for d in cursor:
            d.pop("_id", None)
            out.append(d)
        return out

    async def _db_start_native_watch(
        self, on_event: Callable[[ConfigChangeEvent], Awaitable[None]]
    ) -> None:
        """Phase 8 Step 8.2:启动 Change Stream 后台循环。

        若 collection 没有 ``watch`` 方法(典型场景:测试用 fake client 不打 stream),
        优雅降级为"只记 log",TTL 缓存(默认 5s)兜底。
        """
        if self._native_watch_started:
            return
        watch_fn = getattr(self._coll, "watch", None)
        if not callable(watch_fn):
            logger.info(
                "MongoConfigCenter: collection has no .watch() (likely fake / standalone mongo);"
                " relying on TTL cache (%.1fs).",
                self.overrides_cache_ttl_seconds,
            )
            self._native_watch_started = True
            return

        self._watch_stop = asyncio.Event()
        self._watch_task = asyncio.create_task(
            self._run_change_stream(on_event),
            name="mongo_config_center.change_stream",
        )
        self._native_watch_started = True
        logger.info("MongoConfigCenter: Change Stream watcher started.")

    async def _db_stop_native_watch(self) -> None:
        if self._watch_stop is not None:
            self._watch_stop.set()
        task = self._watch_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("change_stream task exit error: %s", e)
            self._watch_task = None
        self._watch_stop = None
        self._native_watch_started = False

    # ════════════════════════════════════════════════════════════
    # Change Stream 主循环(Phase 8 Step 8.2)
    # ════════════════════════════════════════════════════════════
    async def _run_change_stream(
        self,
        on_event: Callable[[ConfigChangeEvent], Awaitable[None]],
    ) -> None:
        """长循环消费 Change Stream;断流自动指数退避重连。"""
        backoff = WATCH_BACKOFF_INITIAL_SECONDS
        while self._watch_stop is None or not self._watch_stop.is_set():
            try:
                stream_ctx = self._coll.watch(full_document="updateLookup")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "change_stream open failed (retry in %.1fs): %s", backoff, e
                )
                if await self._wait_or_stop(backoff):
                    break
                backoff = min(backoff * 2.0, WATCH_BACKOFF_MAX_SECONDS)
                continue
            try:
                async with stream_ctx as stream:
                    backoff = WATCH_BACKOFF_INITIAL_SECONDS  # 连上即重置退避
                    async for change in stream:
                        if self._watch_stop is not None and self._watch_stop.is_set():
                            break
                        event = self._change_to_event(change)
                        if event is None:
                            continue
                        try:
                            await on_event(event)
                        except Exception as cb_e:  # noqa: BLE001
                            # callback 异常不应影响 stream 主循环
                            logger.warning(
                                "change_stream on_event callback raised: %s", cb_e
                            )
            except asyncio.CancelledError:
                # 上层取消(stop):干净退出
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "change_stream broken (retry in %.1fs): %s", backoff, e
                )
                if await self._wait_or_stop(backoff):
                    break
                backoff = min(backoff * 2.0, WATCH_BACKOFF_MAX_SECONDS)

    async def _wait_or_stop(self, seconds: float) -> bool:
        """等待 ``seconds`` 或 stop event;返回 True 表示应当退出循环。"""
        if self._watch_stop is None:
            await asyncio.sleep(seconds)
            return False
        try:
            await asyncio.wait_for(self._watch_stop.wait(), timeout=seconds)
            return True  # stop 被触发
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _change_to_event(change: dict[str, Any]) -> ConfigChangeEvent | None:
        """把 Mongo Change Stream 事件转成 :class:`ConfigChangeEvent`。

        过滤规则:
        - 仅关心 ``insert / update / replace`` 三种操作
        - 必须能拿到 ``fullDocument``(``update`` 模式下需 ``full_document="updateLookup"``)
        - 缺少 ``category`` 字段视为非 config 文档,跳过
        """
        op = change.get("operationType")
        if op not in ("insert", "update", "replace"):
            return None
        doc = change.get("fullDocument") or {}
        category = doc.get("category")
        if not category:
            return None
        return ConfigChangeEvent(
            category=str(category),
            scope=str(doc.get("scope", "global")),
            scope_id=doc.get("scope_id"),
            name=doc.get("name"),
            version=int(doc.get("version", 0) or 0),
            actor=str(doc.get("actor", "system")),
        )

    async def _db_ping(self) -> bool:
        try:
            await self._db.command("ping")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("mongo ping failed: %s", e)
            return False


__all__ = ["MongoConfigCenter"]
