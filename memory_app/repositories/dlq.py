"""DLQ(Dead Letter Queue)—— 三库同步失败时的降级队列(设计文档 §5.4 / §12.9)。

═══════════════════════════════════════════════════════════════════════════════
为什么需要 DLQ
═══════════════════════════════════════════════════════════════════════════════
设计文档 §5.2 双库一致性契约:**MongoDB 是 SOT,ES + Milvus 是从属索引**。

写入热路径若 ES 或 Milvus 失败:
- **不**回滚 MongoDB(否则一次外部抖动让用户看到 ingest 失败)
- 把不一致项记入 DLQ,由 ``MemorySyncReconciler`` 离线 5min 扫一次重试
- DLQ 重试 ≥ 3 次仍失败 → 告警 + 人工介入

═══════════════════════════════════════════════════════════════════════════════
本模块实现
═══════════════════════════════════════════════════════════════════════════════
- :class:`DLQRecord`            一条 DLQ 记录的数据模型
- :class:`InMemoryDLQ`          进程内 DLQ(测试 / 开发态默认)
- 生产 ``MongoDLQStore`` / ``RedisDLQStore`` 在 Phase 6+ 落地;接口签名同此

═══════════════════════════════════════════════════════════════════════════════
DLQRecord 字段
═══════════════════════════════════════════════════════════════════════════════
``target``       从属索引名(``es`` / ``milvus`` / ``...``)
``mem_cell_id``  失败的 MemCell 主键
``operation``    操作类型(``index`` / ``upsert`` / ``delete``)
``error``        异常摘要(供运维快速定位)
``retry_count``  已重试次数
``timestamp``    入队时间(UTC)
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════════════════════════════════
class DLQRecord(BaseModel):
    """DLQ 一条记录。"""

    model_config = ConfigDict(extra="allow")

    target: str
    mem_cell_id: str
    operation: str = "index"
    error: str = ""
    retry_count: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] | None = None


# ════════════════════════════════════════════════════════════════════════════
# 协议
# ════════════════════════════════════════════════════════════════════════════
class DLQProto(Protocol):
    """DLQ 接口约定;生产 MongoDLQ / RedisDLQ 需满足。"""

    async def enqueue(self, record: DLQRecord) -> None: ...
    async def list(self, target: str | None = None, limit: int = 50) -> list[DLQRecord]: ...
    async def size(self) -> int: ...
    async def remove(self, target: str, mem_cell_id: str) -> bool: ...
    async def bump_retry(
        self, target: str, mem_cell_id: str, *, error: str
    ) -> bool: ...


# ════════════════════════════════════════════════════════════════════════════
# 内存实现
# ════════════════════════════════════════════════════════════════════════════
class InMemoryDLQ:
    """进程内 DLQ。

    用 :class:`collections.deque` 保留**最近 N 条**(默认 1000),溢出时自动丢弃
    最老条目并打 warn —— 表示运维需要立即介入查看为什么积压。

    生产环境**禁用**本实现:进程重启即丢失。仅供:
    - 单元测试
    - 开发态本地启动(无 Mongo / Redis 仍能跑通管线)
    """

    def __init__(self, max_size: int = 1000) -> None:
        self._buffer: deque[DLQRecord] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()

    async def enqueue(self, record: DLQRecord) -> None:
        async with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                logger.warning(
                    "DLQ full (%d), oldest record dropped: target=%s id=%s",
                    self._buffer.maxlen,
                    self._buffer[0].target,
                    self._buffer[0].mem_cell_id,
                )
            self._buffer.append(record)

    async def list(
        self, target: str | None = None, limit: int = 50
    ) -> list[DLQRecord]:
        """读取最近的记录;``target=None`` 时不过滤。"""
        async with self._lock:
            items = list(self._buffer)
        if target is not None:
            items = [r for r in items if r.target == target]
        # 最新优先
        items.reverse()
        return items[:limit]

    async def size(self) -> int:
        async with self._lock:
            return len(self._buffer)

    async def remove(self, target: str, mem_cell_id: str) -> bool:
        async with self._lock:
            kept = [
                r
                for r in self._buffer
                if not (r.target == target and r.mem_cell_id == mem_cell_id)
            ]
            if len(kept) == len(self._buffer):
                return False
            self._buffer = deque(kept, maxlen=self._buffer.maxlen)
            return True

    async def bump_retry(self, target: str, mem_cell_id: str, *, error: str) -> bool:
        async with self._lock:
            for rec in self._buffer:
                if rec.target == target and rec.mem_cell_id == mem_cell_id:
                    rec.retry_count += 1
                    rec.error = error
                    return True
        return False

    def clear(self) -> None:
        """清空。仅供测试。"""
        self._buffer.clear()


__all__ = ["DLQRecord", "DLQProto", "InMemoryDLQ"]
