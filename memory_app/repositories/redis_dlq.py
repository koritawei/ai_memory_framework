"""Redis 持久化 DLQ（LIST + JSON）。"""

from __future__ import annotations

import json
import logging
from typing import Any

from memory_app.repositories.dlq import DLQRecord

logger = logging.getLogger(__name__)

DEFAULT_REDIS_DLQ_KEY = "memory:dlq"


class RedisDLQ:
    """Redis LIST 持久化 DLQ；``LPUSH`` 入队，``LRANGE`` 读取。"""

    def __init__(
        self,
        redis_client: Any,
        *,
        key: str = DEFAULT_REDIS_DLQ_KEY,
        max_size: int = 10_000,
    ) -> None:
        self._redis = redis_client
        self._key = key
        self._max_size = max(1, int(max_size))

    async def enqueue(self, record: DLQRecord) -> None:
        payload = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        await self._redis.lpush(self._key, payload)
        try:
            await self._redis.ltrim(self._key, 0, self._max_size - 1)
        except Exception as e:  # noqa: BLE001
            logger.warning("redis dlq ltrim failed: %s", e)

    async def list(
        self, target: str | None = None, limit: int = 50
    ) -> list[DLQRecord]:
        raw_items = await self._redis.lrange(self._key, 0, self._max_size - 1)
        out: list[DLQRecord] = []
        for raw in raw_items or []:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                data = json.loads(raw)
                rec = DLQRecord.model_validate(data)
            except Exception:  # noqa: BLE001
                continue
            if target is not None and rec.target != target:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    async def size(self) -> int:
        return int(await self._redis.llen(self._key))

    async def remove(self, target: str, mem_cell_id: str) -> bool:
        items = await self._redis.lrange(self._key, 0, -1)
        for raw in items or []:
            raw_for_lrem = raw
            raw_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            try:
                data = json.loads(raw_str)
            except json.JSONDecodeError:
                continue
            if data.get("target") == target and data.get("mem_cell_id") == mem_cell_id:
                await self._redis.lrem(self._key, 1, raw_for_lrem)
                return True
        return False

    async def bump_retry(self, target: str, mem_cell_id: str, *, error: str) -> bool:
        items = await self._redis.lrange(self._key, 0, -1)
        for raw in items or []:
            raw_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            try:
                data = json.loads(raw_str)
            except json.JSONDecodeError:
                continue
            if data.get("target") != target or data.get("mem_cell_id") != mem_cell_id:
                continue
            data["retry_count"] = int(data.get("retry_count", 0)) + 1
            data["error"] = error
            await self._redis.lrem(self._key, 1, raw)
            await self._redis.lpush(self._key, json.dumps(data, ensure_ascii=False))
            return True
        return False


__all__ = ["RedisDLQ", "DEFAULT_REDIS_DLQ_KEY"]
