"""MemCell 在 MongoDB 的持久化层。

═══════════════════════════════════════════════════════════════════════════════
角色:SOT(Source of Truth)
═══════════════════════════════════════════════════════════════════════════════
按  三库映射,MongoDB 是 MemCell 的**唯一**真值源:
- 写入热路径首先落 MongoDB;成功后再同步 ES + Milvus
- ES / Milvus 数据丢失时,可由 MongoDB 重建索引
- DLQ 记录不一致项,由离线 Reconciler 5min 扫一次重试

═══════════════════════════════════════════════════════════════════════════════
索引建议(写入热路径 启动期通过 ``ensure_indexes`` 幂等创建)
═══════════════════════════════════════════════════════════════════════════════
- 主键 ``mem_cell_id`` 唯一索引(避免 PK 冲突)
- ``(tenant_id, user_id, created_at)`` 复合索引(按用户拉时间线)
- ``state`` 索引(过滤 HOT/COLD 等状态)
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from memory_app._compat import utcnow
from memory_app.internal_models import MemCell

logger = logging.getLogger(__name__)


class MongoMemCellRepo:
    """MemCell 的 MongoDB CRUD 仓储。

    构造接收 ``motor.motor_asyncio.AsyncIOMotorDatabase`` 实例;为避免
    硬依赖 motor,类型注解保持宽松(支持 fake DB 单测)。
    """

    def __init__(self, db: Any, collection_name: str = "mem_cells") -> None:
        # 延迟通过 ``__getitem__`` 拿 collection;motor 与 fake mongo 都支持
        self._db = db
        self._collection_name = collection_name
        self.collection = db[collection_name]

    # ════════════════════════════════════════════════════════════════════════
    # 启动期索引
    # ════════════════════════════════════════════════════════════════════════
    async def ensure_indexes(self) -> None:
        """启动期幂等建索引。失败仅 warn,不阻塞 ingest 路径。"""
        try:
            await self.collection.create_index(
                [("mem_cell_id", 1)], unique=True, name="uniq_mem_cell_id"
            )
            await self.collection.create_index(
                [("tenant_id", 1), ("user_id", 1), ("created_at", -1)],
                name="idx_tenant_user_created",
            )
            await self.collection.create_index([("state", 1)], name="idx_state")
        except Exception as e:  # noqa: BLE001
            logger.warning("ensure_indexes failed (degraded): %s", e)

    # ════════════════════════════════════════════════════════════════════════
    # 写入
    # ════════════════════════════════════════════════════════════════════════
    async def insert(self, cell: MemCell) -> str:
        """插入一条 MemCell,返回 ``mem_cell_id``。

        :raises pymongo.errors.DuplicateKeyError: 主键冲突(罕见,UUID 命中概率极低)
        """
        doc = cell.model_dump(mode="json")
        await self.collection.insert_one(doc)
        return cell.mem_cell_id

    async def insert_many(self, cells: Iterable[MemCell]) -> list[str]:
        """批量插入。失败抛原异常;调用方根据 ``ordered=False`` 做部分重试。"""
        cells_list = list(cells)
        if not cells_list:
            return []
        docs = [c.model_dump(mode="json") for c in cells_list]
        await self.collection.insert_many(docs, ordered=False)
        return [c.mem_cell_id for c in cells_list]

    # ════════════════════════════════════════════════════════════════════════
    # 读取
    # ════════════════════════════════════════════════════════════════════════
    async def get_by_id(self, mem_cell_id: str) -> MemCell | None:
        """按主键查;不存在返回 None。"""
        doc = await self.collection.find_one({"mem_cell_id": mem_cell_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return MemCell.model_validate(doc)

    async def get_by_ids(self, mem_cell_ids: list[str]) -> list[MemCell]:
        """批量查 —— 一次 ``{$in:[…]}`` 替代 N 次 ``find_one``。

        - 顺序按入参 ``mem_cell_ids`` 保留(底层 cursor 返回顺序不保证)
        - 不存在的 id 被静默丢弃,**不**返回 ``None`` 占位 ——
          调用方按 id 自行映射 ``{m.mem_cell_id: m for m in result}``
        """
        if not mem_cell_ids:
            return []
        ids = list(dict.fromkeys(mem_cell_ids))  # 去重保序
        cursor = self.collection.find({"mem_cell_id": {"$in": ids}})
        if hasattr(cursor, "to_list"):
            docs = await cursor.to_list(length=len(ids))
        else:
            docs = list(cursor)
        cells = [MemCell.model_validate(_strip_id(d)) for d in docs]
        by_id = {c.mem_cell_id: c for c in cells}
        return [by_id[i] for i in ids if i in by_id]

    # ════════════════════════════════════════════════════════════════════════
    # 更新
    # ════════════════════════════════════════════════════════════════════════
    async def update(self, mem_cell_id: str, updates: dict[str, Any]) -> bool:
        """部分字段更新。返回是否真有文档被改动。"""
        result = await self.collection.update_one(
            {"mem_cell_id": mem_cell_id}, {"$set": updates}
        )
        return bool(getattr(result, "modified_count", 0))

    # ════════════════════════════════════════════════════════════════════════
    # 批量状态变更(离线巩固 巩固 / 容量回收时用)
    # ════════════════════════════════════════════════════════════════════════
    async def bulk_set_state(
        self,
        mem_cell_ids: list[str],
        new_state: Any,
    ) -> int:
        """把一批 cell 的 ``state`` 字段一次性置为 ``new_state``。

        替代 ``for mid in ids: await update(mid, {"state": ...})`` 的 N 次
        round-trip。``new_state`` 支持 :class:`MemoryState` 枚举或 raw string。

        :returns: 实际修改条数
        """
        if not mem_cell_ids:
            return 0
        state_value = new_state.value if hasattr(new_state, "value") else str(new_state)
        result = await self.collection.update_many(
            {"mem_cell_id": {"$in": list(mem_cell_ids)}},
            {"$set": {"state": state_value, "updated_at": utcnow()}},
        )
        return int(getattr(result, "modified_count", 0))

    # ════════════════════════════════════════════════════════════════════════
    # 批量生命周期更新(反馈与生命周期,检索命中后批量 +access)
    # ════════════════════════════════════════════════════════════════════════
    async def atomic_apply_strength_delta(
        self,
        mem_cell_id: str,
        *,
        delta: float,
        s_max: float,
        increment_access: bool,
    ) -> dict | None:
        """原子化地应用 ``strength += delta``(裁剪到 ``s_max``)与可选 ``access++``。

        替代旧版"``get_by_id`` → Python 计算 → ``update``"读改写,
        消除两请求并发时各自基于陈旧值更新导致的丢失更新。

        利用 Mongo 4.2+ aggregation-pipeline update 在服务端做条件加法 + 裁剪;
        ``find_one_and_update`` 返回更新后的文档,客户端拿到的就是落库后的事实值。

        :returns: ``{"strength": new, "access_count": new}`` 或 None(目标不存在)
        """
        now = utcnow()
        sets: dict = {
            "strength": {
                "$min": [
                    {"$add": [{"$ifNull": ["$strength", 0.0]}, float(delta)]},
                    float(s_max),
                ]
            },
            "updated_at": now,
        }
        if increment_access:
            sets["access_count"] = {
                "$add": [{"$ifNull": ["$access_count", 0]}, 1]
            }
        pipeline = [{"$set": sets}]
        coll = self.collection
        if hasattr(coll, "find_one_and_update"):
            try:
                from pymongo import ReturnDocument
                doc = await coll.find_one_and_update(
                    {"mem_cell_id": mem_cell_id},
                    pipeline,
                    return_document=ReturnDocument.AFTER,
                )
            except ImportError:
                # 测试 fake 无 pymongo 依赖时,降级为 update + get
                await coll.update_one({"mem_cell_id": mem_cell_id}, pipeline)
                cell = await self.get_by_id(mem_cell_id)
                if cell is None:
                    return None
                return {
                    "strength": float(cell.strength),
                    "access_count": int(cell.access_count),
                }
            if doc is None:
                return None
            return {
                "strength": float(doc.get("strength", 0.0)),
                "access_count": int(doc.get("access_count", 0)),
            }
        # 极度退化:无 find_one_and_update,串行 update + get(原子性弱)
        await coll.update_one({"mem_cell_id": mem_cell_id}, pipeline)
        cell = await self.get_by_id(mem_cell_id)
        if cell is None:
            return None
        return {
            "strength": float(cell.strength),
            "access_count": int(cell.access_count),
        }

    async def bulk_increment_access(
        self,
        mem_cell_ids: list[str],
        *,
        strength_delta: float,
        s_max: float,
    ) -> int:
        """单次 Mongo round-trip 完成 N 条 ``access_count + 1`` 与
        ``strength = min(strength + delta, s_max)``。

        替代旧版 N 个 ``get_by_id + update`` 的 2N 次 round-trip,**显著**
        降低高 QPS 下检索完成阶段的 Mongo 负载。

        约定:
        - 用 Mongo 4.2+ aggregation pipeline update 一次原子完成 cap + inc
        - 不重算 ``state`` 字段(避免 N 次 ``compute_state``);留待下次显式更新
          或离线 reconciler 修正 —— state 的"陈旧"语义可被接受(它只是 hint)
        - 调用方应在 fire-and-forget 后台任务中执行,异常不上抛业务平面

        :returns: 实际被修改的文档数
        """
        if not mem_cell_ids:
            return 0
        now = utcnow()
        pipeline = [
            {
                "$set": {
                    "strength": {
                        "$min": [
                            {"$add": [{"$ifNull": ["$strength", 0.0]}, float(strength_delta)]},
                            float(s_max),
                        ]
                    },
                    "access_count": {
                        "$add": [{"$ifNull": ["$access_count", 0]}, 1]
                    },
                    "updated_at": now,
                }
            },
        ]
        result = await self.collection.update_many(
            {"mem_cell_id": {"$in": list(mem_cell_ids)}}, pipeline
        )
        return int(getattr(result, "modified_count", 0))

    # ════════════════════════════════════════════════════════════════════════
    # 删除
    # ════════════════════════════════════════════════════════════════════════
    async def delete_by_id(self, mem_cell_id: str) -> bool:
        """按主键删除。返回是否真删了。"""
        result = await self.collection.delete_one({"mem_cell_id": mem_cell_id})
        return bool(getattr(result, "deleted_count", 0))

    # ════════════════════════════════════════════════════════════════════════
    # 离线巩固 离线巩固查询接口
    # ════════════════════════════════════════════════════════════════════════
    async def find_by_state(
        self,
        tenant_id: str,
        user_id: str,
        state: Any,
        limit: int = 1000,
    ) -> list[MemCell]:
        """按 ``state`` 过滤。支持枚举 / 字符串。"""
        state_value = state.value if hasattr(state, "value") else str(state)
        cursor = self.collection.find(
            {"tenant_id": tenant_id, "user_id": user_id, "state": state_value}
        )
        # motor 异步游标 + Fake collection 同步迭代;两种都用 to_list
        if hasattr(cursor, "to_list"):
            docs = await cursor.to_list(length=limit)
        else:
            docs = list(cursor)[:limit]
        return [MemCell.model_validate(_strip_id(d)) for d in docs]

    async def count(self, tenant_id: str, user_id: str) -> int:
        """统计 tenant + user 下的 MemCell 总数。"""
        coll = self.collection
        if hasattr(coll, "count_documents"):
            return int(await coll.count_documents({"tenant_id": tenant_id, "user_id": user_id}))
        # fallback:遍历 store(测试 fake)
        return int(await _count_via_find(coll, {"tenant_id": tenant_id, "user_id": user_id}))

    async def find_all(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 10000,
    ) -> list[MemCell]:
        """拉取 tenant + user 下的所有 MemCell(用于容量约束扫描)。"""
        cursor = self.collection.find({"tenant_id": tenant_id, "user_id": user_id})
        if hasattr(cursor, "to_list"):
            docs = await cursor.to_list(length=limit)
        else:
            docs = list(cursor)[:limit]
        return [MemCell.model_validate(_strip_id(d)) for d in docs]


def _strip_id(doc: dict[str, Any]) -> dict[str, Any]:
    if isinstance(doc, dict):
        out = dict(doc)
        out.pop("_id", None)
        return out
    return doc


async def _count_via_find(coll: Any, filt: dict) -> int:
    """fallback:通过 find.to_list 计数(motor / fake 通吃)。"""
    cursor = coll.find(filt)
    if hasattr(cursor, "to_list"):
        return len(await cursor.to_list(length=10**6))
    return len(list(cursor))


__all__ = ["MongoMemCellRepo"]
