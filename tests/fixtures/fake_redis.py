"""内存 Redis LIST / ZSET / STRING 模拟 —— 供 RedisTaskRunner 与分布式锁测试。"""

from __future__ import annotations

import asyncio
import time


class FakeRedisLists:
    """RPUSH / LPUSH / BRPOPLPUSH / LREM / RPOP / ZSET / SET NX，带 asyncio.Lock。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.kv: dict[str, tuple[str, float | None]] = {}  # value, expire_at|None
        self._lock = asyncio.Lock()

    async def rpush(self, key: str, value: str) -> int:
        async with self._lock:
            self.lists.setdefault(key, []).append(value)
            return len(self.lists[key])

    async def lpush(self, key: str, value: str) -> int:
        async with self._lock:
            self.lists.setdefault(key, []).insert(0, value)
            return len(self.lists[key])

    async def brpoplpush(self, src: str, dst: str, timeout: int = 0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, float(timeout))
        while True:
            async with self._lock:
                src_list = self.lists.get(src, [])
                if src_list:
                    val = src_list.pop(0)
                    self.lists.setdefault(dst, []).append(val)
                    return val
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.005)

    async def lrem(self, key: str, count: int, value: str) -> int:
        async with self._lock:
            lst = self.lists.get(key, [])
            removed = 0
            while count != 0 and value in lst:
                lst.remove(value)
                removed += 1
                if count > 0:
                    count -= 1
            return removed

    async def rpop(self, key: str):
        async with self._lock:
            lst = self.lists.get(key, [])
            if not lst:
                return None
            return lst.pop()

    async def llen(self, key: str) -> int:
        async with self._lock:
            return len(self.lists.get(key, []))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        async with self._lock:
            z = self.zsets.setdefault(key, {})
            added = 0
            for member, score in mapping.items():
                if member not in z:
                    added += 1
                z[member] = float(score)
            return added

    async def zrangebyscore(
        self, key: str, min_score: float, max_score: float
    ) -> list[str]:
        async with self._lock:
            z = self.zsets.get(key, {})
            return [
                m
                for m, s in z.items()
                if float(min_score) <= s <= float(max_score)
            ]

    async def zrem(self, key: str, *members: str) -> int:
        async with self._lock:
            z = self.zsets.get(key, {})
            removed = 0
            for m in members:
                if m in z:
                    del z[m]
                    removed += 1
            return removed

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
        xx: bool = False,
    ) -> bool | None:
        async with self._lock:
            self._expire_kv_unlocked()
            exists = key in self.kv
            if nx and exists:
                return False
            if xx and not exists:
                return False
            expire_at = (time.time() + ex) if ex else None
            self.kv[key] = (str(value), expire_at)
            return True

    async def get(self, key: str) -> str | None:
        async with self._lock:
            self._expire_kv_unlocked()
            item = self.kv.get(key)
            return item[0] if item else None

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            removed = 0
            for k in keys:
                if k in self.kv:
                    del self.kv[k]
                    removed += 1
                if k in self.lists:
                    del self.lists[k]
                    removed += 1
                if k in self.zsets:
                    del self.zsets[k]
                    removed += 1
            return removed

    def _expire_kv_unlocked(self) -> None:
        now = time.time()
        expired = [
            k
            for k, (_v, exp) in self.kv.items()
            if exp is not None and exp <= now
        ]
        for k in expired:
            del self.kv[k]
