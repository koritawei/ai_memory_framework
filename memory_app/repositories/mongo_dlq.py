"""MongoDB 持久化 DLQ。"""

from __future__ import annotations

import logging
from typing import Any

from memory_app.repositories.dlq import DLQRecord

logger = logging.getLogger(__name__)


class MongoDLQ:
    """将 DLQ 记录写入 MongoDB 集合 ``dlq_records``。"""

    def __init__(self, db: Any, collection_name: str = "dlq_records") -> None:
        self.collection = db[collection_name]

    async def ensure_indexes(self) -> None:
        try:
            await self.collection.create_index(
                [("timestamp", -1)], name="idx_dlq_timestamp"
            )
            await self.collection.create_index(
                [("target", 1), ("timestamp", -1)], name="idx_dlq_target_ts"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("mongo dlq ensure_indexes failed: %s", e)

    async def enqueue(self, record: DLQRecord) -> None:
        doc = record.model_dump(mode="json")
        await self.collection.insert_one(doc)

    async def list(
        self, target: str | None = None, limit: int = 50
    ) -> list[DLQRecord]:
        filt: dict[str, Any] = {}
        if target is not None:
            filt["target"] = target
        cursor = self.collection.find(filt).sort("timestamp", -1).limit(limit)
        if hasattr(cursor, "to_list"):
            docs = await cursor.to_list(length=limit)
        else:
            docs = list(cursor)[:limit]
        out: list[DLQRecord] = []
        for doc in docs:
            if isinstance(doc, dict):
                doc.pop("_id", None)
            out.append(DLQRecord.model_validate(doc))
        return out

    async def size(self) -> int:
        coll = self.collection
        if hasattr(coll, "count_documents"):
            return int(await coll.count_documents({}))
        return len(await self.list(limit=10_000))

    async def remove(self, target: str, mem_cell_id: str) -> bool:
        result = await self.collection.delete_one(
            {"target": target, "mem_cell_id": mem_cell_id}
        )
        return bool(getattr(result, "deleted_count", 0))

    async def bump_retry(self, target: str, mem_cell_id: str, *, error: str) -> bool:
        from memory_app._compat import utcnow

        result = await self.collection.update_one(
            {"target": target, "mem_cell_id": mem_cell_id},
            {
                "$inc": {"retry_count": 1},
                "$set": {"error": error, "timestamp": utcnow()},
            },
        )
        return bool(getattr(result, "matched_count", 0))


__all__ = ["MongoDLQ"]
