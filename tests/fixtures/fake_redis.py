"""内存 Redis LIST 模拟 —— 供 RedisTaskRunner 单元 / 集成测试。"""

from __future__ import annotations

import asyncio


class FakeRedisLists:
    """RPUSH / LPUSH / BRPOPLPUSH / LREM / RPOP，带 asyncio.Lock 保证并发安全。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
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
