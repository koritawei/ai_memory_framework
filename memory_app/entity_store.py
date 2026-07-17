"""EntityStore —— entity→mem_cell_ids 倒排索引(设计文档 §5.3)。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
- 提供"实体名 → 关联记忆 ID 集合"的倒排索引,供 :class:`EntityChannel` 召回
- 与 :class:`memory_app.plugins.spi.graph_store.GraphStore` SPI **不同**:
  - EntityStore:简单倒排索引,O(1) 查找,Phase 7 入门级
  - GraphStore:多类型节点 / 边 + 遍历,Phase 7 高阶能力(Step 7.3)
- 不是 SPI 插件,仅为业务组件;Mongo 后端 + dict fallback(测试)

═══════════════════════════════════════════════════════════════════════════════
文档结构
═══════════════════════════════════════════════════════════════════════════════
集合名:``entities``。每条 doc:

::

    {
      "_id": "<auto>",
      "entity": "北京",
      "tenant_id": "t1",
      "user_id": "u1",
      "mem_cell_ids": ["mc1", "mc2", ...],
      "updated_at": <datetime>,
      "ref_count": 2,
    }

主键唯一约束:``(entity, tenant_id, user_id)``;``addToSet`` 保证不重复。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Mongo 后端
# ════════════════════════════════════════════════════════════════════════════
class EntityStore:
    """Mongo 后端的 entity 倒排索引。

    构造接收 ``motor`` 的 db 实例;为单测便利,类型注解保持宽松。
    """

    def __init__(self, db: Any, collection_name: str = "entities") -> None:
        self._db = db
        self._collection_name = collection_name
        self.collection = db[collection_name]

    # ════════════════════════════════════════════════════════════════════════
    # 启动期索引
    # ════════════════════════════════════════════════════════════════════════
    async def ensure_indexes(self) -> None:
        """启动期幂等建索引;失败仅 warn。"""
        try:
            await self.collection.create_index(
                [("entity", 1), ("tenant_id", 1), ("user_id", 1)],
                unique=True,
                name="uniq_entity_tenant_user",
            )
            await self.collection.create_index(
                [("tenant_id", 1), ("user_id", 1), ("updated_at", -1)],
                name="idx_tenant_user_updated",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("entity_store ensure_indexes failed (degraded): %s", e)

    # ════════════════════════════════════════════════════════════════════════
    # 写入
    # ════════════════════════════════════════════════════════════════════════
    async def upsert_entities(
        self,
        mem_cell_id: str,
        entities: Iterable[str],
        tenant_id: str,
        user_id: str,
    ) -> int:
        """为每个 entity upsert mem_cell_id;返回成功 upsert 的实体数。

        语义:``addToSet`` 保证幂等,同一 mem_cell_id + 实体多次写入不重复。

        实现:优先用 ``bulk_write`` 把 N 次 ``update_one`` 合并为 1 次 round-trip
        (典型 cell 含 5–20 entities,N×RTT → 1×RTT,显著降低 Mongo 负载)。
        驱动 / fake repo 不支持 ``bulk_write`` 时退化到旧的逐条 update。
        """
        ents = _dedupe_entities(entities)
        if not ents:
            return 0
        now = _utcnow()
        bulk_write_fn = getattr(self.collection, "bulk_write", None)
        if callable(bulk_write_fn):
            try:
                from pymongo import UpdateOne
            except ImportError:
                bulk_write_fn = None
        if callable(bulk_write_fn):
            ops = [
                UpdateOne(
                    {"entity": ent, "tenant_id": tenant_id, "user_id": user_id},
                    {
                        "$addToSet": {"mem_cell_ids": mem_cell_id},
                        "$set": {"updated_at": now},
                        "$setOnInsert": {
                            "entity": ent,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                        },
                    },
                    upsert=True,
                )
                for ent in ents
            ]
            try:
                # ordered=False:单条失败不阻塞其他;贴近 Mongo bulk 默认建议
                result = await self.collection.bulk_write(ops, ordered=False)
                # bulk_write 的精确"成功数"较复杂(matched / modified / upserted);
                # 这里按"提交条数"返回,与旧实现的"尝试条数"语义对齐。
                _ = result
                return len(ents)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "entity_store bulk_write upsert failed, fallback per-entity: %s", e
                )
        # 退化:逐条 update
        success = 0
        for ent in ents:
            try:
                await self.collection.update_one(
                    {"entity": ent, "tenant_id": tenant_id, "user_id": user_id},
                    {
                        "$addToSet": {"mem_cell_ids": mem_cell_id},
                        "$set": {"updated_at": now},
                        "$setOnInsert": {
                            "entity": ent,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                        },
                    },
                    upsert=True,
                )
                success += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "entity_store upsert failed (%s): %s", ent, e
                )
        return success

    # ════════════════════════════════════════════════════════════════════════
    # 查询
    # ════════════════════════════════════════════════════════════════════════
    async def find_by_entities(
        self,
        entities: Iterable[str],
        tenant_id: str,
        user_id: str,
        *,
        limit: int = 1000,
    ) -> list[str]:
        """返回与 entities 任一关联的 ``mem_cell_id`` 去重列表。"""
        ents = _dedupe_entities(entities)
        if not ents:
            return []
        cursor = self.collection.find(
            {
                "entity": {"$in": list(ents)},
                "tenant_id": tenant_id,
                "user_id": user_id,
            }
        )
        seen: set[str] = set()
        if hasattr(cursor, "to_list"):
            docs = await cursor.to_list(length=limit)
        else:
            docs = list(cursor)[:limit]
        for doc in docs:
            for mid in doc.get("mem_cell_ids", []) or []:
                seen.add(mid)
        return list(seen)

    async def find_by_entity(
        self,
        entity: str,
        tenant_id: str,
        user_id: str,
    ) -> list[str]:
        """单实体便利方法。"""
        return await self.find_by_entities([entity], tenant_id, user_id)

    async def remove_mem_cell(
        self,
        mem_cell_id: str,
        tenant_id: str,
        user_id: str,
    ) -> int:
        """从所有实体的 mem_cell_ids 中移除 mem_cell_id;返回受影响实体数。

        Phase 7 简化:Phase 6 离线巩固归档记忆时,EntityIndexStage 走 ARCHIVED
        路径会复用本方法;Phase 7 暂不连入,仅暴露接口。
        """
        try:
            result = await self.collection.update_many(
                {"tenant_id": tenant_id, "user_id": user_id},
                {"$pull": {"mem_cell_ids": mem_cell_id}},
            )
            return int(getattr(result, "modified_count", 0))
        except Exception as e:  # noqa: BLE001
            logger.warning("entity_store remove_mem_cell failed: %s", e)
            return 0


# ════════════════════════════════════════════════════════════════════════════
# 内存版(测试 / 开发态便利)
# ════════════════════════════════════════════════════════════════════════════
class InMemoryEntityStore:
    """进程内 EntityStore。语义与 :class:`EntityStore` 一致。

    仅供单测 / 离线评测使用;生产环境禁用(进程重启即丢失)。
    """

    def __init__(self) -> None:
        # (tenant, user, entity) → set[mem_cell_id]
        self._index: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    async def ensure_indexes(self) -> None:
        return None

    async def upsert_entities(
        self,
        mem_cell_id: str,
        entities: Iterable[str],
        tenant_id: str,
        user_id: str,
    ) -> int:
        ents = _dedupe_entities(entities)
        for ent in ents:
            self._index[(tenant_id, user_id, ent)].add(mem_cell_id)
        return len(ents)

    async def find_by_entities(
        self,
        entities: Iterable[str],
        tenant_id: str,
        user_id: str,
        *,
        limit: int = 1000,
    ) -> list[str]:
        ents = _dedupe_entities(entities)
        out: set[str] = set()
        for ent in ents:
            out.update(self._index.get((tenant_id, user_id, ent), set()))
        return list(out)[:limit]

    async def find_by_entity(
        self, entity: str, tenant_id: str, user_id: str
    ) -> list[str]:
        return list(self._index.get((tenant_id, user_id, entity), set()))

    async def remove_mem_cell(
        self, mem_cell_id: str, tenant_id: str, user_id: str
    ) -> int:
        affected = 0
        for key, ids in self._index.items():
            if key[0] == tenant_id and key[1] == user_id and mem_cell_id in ids:
                ids.discard(mem_cell_id)
                affected += 1
        return affected


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def _dedupe_entities(entities: Iterable[str]) -> list[str]:
    """去掉空白 / 全空字符串 + 去重 + 保留首次出现顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in entities or []:
        e = (raw or "").strip()
        if not e or e in seen:
            continue
        seen.add(e)
        out.append(e)
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["EntityStore", "InMemoryEntityStore"]
