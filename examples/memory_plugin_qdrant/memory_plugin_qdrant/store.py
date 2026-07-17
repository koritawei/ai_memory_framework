"""QdrantVectorStore —— 第三方 VectorStore 示例(Phase 8 Step 8.4)。

═══════════════════════════════════════════════════════════════════════════════
关键点
═══════════════════════════════════════════════════════════════════════════════
- 通过 ``@register`` 装饰器或 entry_points 接入 PluginRegistry
- 实现 ``memory_app.plugins.spi.vector_store.VectorStore`` 完整契约
- ``meta.config_schema`` 暴露所有可调参数,经 :class:`PluginFactory` 校验后
  注入 ``start(config)``
- 健康检查 ``health()`` 反馈 collection 维度 / 数量等运维 metrics

═══════════════════════════════════════════════════════════════════════════════
为什么是进程内字典而不是真 Qdrant
═══════════════════════════════════════════════════════════════════════════════
本仓 CI 不依赖外部服务;示例的目标是"演示插件接入流程",而不是给生产 Qdrant
做完整客户端。生产替换路径:

1. ``pip install qdrant-client``
2. 把 ``self._collections: dict[str, dict]`` 换成 ``QdrantClient`` 实例
3. ``upsert / search / delete / flush`` 直接转发给 ``QdrantClient`` 同名方法

业务代码 / Pipeline 不变 —— 这就是 SPI 等价性的实际效果。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping

from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.vector_store import (
    VectorHit,
    VectorItem,
    VectorStore,
)

logger = logging.getLogger(__name__)


@register
class QdrantVectorStore(VectorStore):
    """演示用 VectorStore;进程内字典 + 余弦相似度。"""

    meta = PluginMeta(
        name="qdrant_store",
        category="memory.storage.vector",
        version="0.1.0",
        description="第三方插件示例(Phase 8.4):进程内字典 + 余弦,真生产替换为 qdrant-client",
        author="Memory Service Examples",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                # 真生产 Qdrant 客户端的连接参数,本示例仅占位
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 6333},
                "metric": {
                    "type": "string",
                    "enum": ["cosine", "dot", "euclid"],
                    "default": "cosine",
                },
            },
        },
    )

    def __init__(self) -> None:
        # collection_name → {item_id: (vector, payload)}
        self._collections: dict[str, dict[str, tuple[list[float], dict]]] = {}
        self._metric: str = "cosine"
        self._started: bool = False

    # ════════════════════════════════════════════════════════════════════════
    # Plugin 生命周期
    # ════════════════════════════════════════════════════════════════════════
    async def start(self, config: Mapping[str, Any]) -> None:
        self._metric = str(config.get("metric", "cosine")).lower()
        self._started = True
        logger.info(
            "qdrant_store started: metric=%s host=%s port=%s",
            self._metric, config.get("host"), config.get("port"),
        )

    async def stop(self) -> None:
        self._collections.clear()
        self._started = False

    async def health(self) -> dict:
        if not self._started:
            return {"status": "fail", "detail": "not started"}
        return {
            "status": "ok",
            "collections": len(self._collections),
            "total_items": sum(len(c) for c in self._collections.values()),
        }

    # ════════════════════════════════════════════════════════════════════════
    # VectorStore 契约
    # ════════════════════════════════════════════════════════════════════════
    async def upsert(self, collection: str, items: list[VectorItem]) -> None:
        if not items:
            return
        # 维度一致性校验
        first_dim = len(items[0].vector)
        for it in items:
            if len(it.vector) != first_dim:
                from memory_app.plugins.base import PluginError, PluginErrorCategory

                raise PluginError(
                    PluginErrorCategory.CONFIG,
                    "dim_mismatch",
                    f"qdrant_store upsert: dim mismatch ({len(it.vector)} vs {first_dim})",
                )
        bucket = self._collections.setdefault(collection, {})
        for it in items:
            bucket[it.id] = (list(it.vector), dict(it.payload))

    async def search(
        self,
        collection: str,
        query_vec: list[float],
        k: int,
        filters: dict | None = None,
    ) -> list[VectorHit]:
        bucket = self._collections.get(collection)
        if not bucket:
            return []  # 索引未建立 → 返回空(契约约定:不抛)
        scored: list[tuple[float, str, dict]] = []
        for item_id, (vec, payload) in bucket.items():
            if filters and not _payload_matches(payload, filters):
                continue
            score = _similarity(query_vec, vec, self._metric)
            scored.append((score, item_id, payload))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            VectorHit(id=item_id, score=float(score), payload=dict(payload))
            for score, item_id, payload in scored[: max(0, int(k))]
        ]

    async def delete(self, collection: str, ids: list[str]) -> int:
        bucket = self._collections.get(collection)
        if not bucket:
            return 0
        deleted = 0
        for i in ids:
            if i in bucket:
                bucket.pop(i)
                deleted += 1
        return deleted

    async def flush(self, collection: str) -> None:
        # 进程内实现无需 flush;真 Qdrant 客户端转发到 collection.flush()
        return None


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def _payload_matches(payload: dict, filters: dict) -> bool:
    for k, v in filters.items():
        if payload.get(k) != v:
            return False
    return True


def _similarity(a: list[float], b: list[float], metric: str) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    if metric == "dot":
        return sum(x * y for x, y in zip(a, b))
    if metric == "euclid":
        d = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
        return -d  # 越近分越高
    # 默认 cosine
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


__all__ = ["QdrantVectorStore"]
