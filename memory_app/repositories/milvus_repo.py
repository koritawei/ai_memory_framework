"""MemCell 在 Milvus 的向量索引层(设计文档 §5.2 / §6.1)。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
Milvus 承载稠密向量检索;Phase 4 起被 ``VectorChannel`` 用作多路召回之一。

═══════════════════════════════════════════════════════════════════════════════
Phase 2 简化范围
═══════════════════════════════════════════════════════════════════════════════
- ``insert(mem_cell_id, embedding, metadata)`` 接口对接 ``IngestService``
- 仅当 ``cell.embedding`` 不为 None 时才被调用(Phase 2 多数 MemCell 没有
  embedding,因为冷路径 / EmbeddingProvider 在 Phase 3 才写入)
- 真实集合的创建 / 索引参数(IVF_FLAT / HNSW)在 Phase 4 启动期统一管理;
  此处仅做"写入存在的 collection"

═══════════════════════════════════════════════════════════════════════════════
为什么不直接依赖 pymilvus
═══════════════════════════════════════════════════════════════════════════════
设计文档要求 Milvus 是可替换插件(可切 ``qdrant_store`` / ``pgvector_store``)。
本类只暴露最小数据访问语义(insert / delete / search 入口),具体连接 / 集合
由 :data:`memory_app.deps.app_state` 在 lifespan 中维护。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MilvusMemCellRepo:
    """Milvus MemCell 向量索引仓储。

    Phase 2 阶段为简化:
    - 集合名通过构造注入(``collection_name``,默认 ``memory_vectors``)
    - 写入失败抛原异常;:class:`IngestService` 据此走 DLQ 降级
    - 不在本类内做 ``Collection.flush()`` —— 由 lifespan 统一管理

    可注入 ``insert_callable`` 接管真实写入逻辑,便于测试。
    """

    def __init__(
        self,
        collection_name: str = "memory_vectors",
        *,
        insert_callable: Any = None,
        delete_callable: Any = None,
    ) -> None:
        self.collection_name = collection_name
        self._insert_callable = insert_callable
        self._delete_callable = delete_callable

    # ════════════════════════════════════════════════════════════════════════
    # 写入
    # ════════════════════════════════════════════════════════════════════════
    async def insert(
        self,
        mem_cell_id: str,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """插入一条向量。

        若构造时未注入 ``insert_callable``,本方法回退到调用 ``pymilvus``
        的 ``Collection.insert``(运行时 import,避免测试期硬依赖 Milvus)。
        """
        if not embedding:
            # 上层应在 cell.embedding=None 时跳过本调用;这里防御性 noop
            logger.debug("milvus insert skipped: empty embedding for %s", mem_cell_id)
            return
        if self._insert_callable is not None:
            await _maybe_await(
                self._insert_callable(mem_cell_id, embedding, metadata or {})
            )
            return
        # 真实路径(运行时 lazy import pymilvus)
        try:
            from pymilvus import Collection  # noqa: WPS433
        except ImportError as e:
            raise RuntimeError(
                "pymilvus not installed; provide insert_callable for tests"
            ) from e
        col = Collection(self.collection_name)
        record = {
            "mem_cell_id": mem_cell_id,
            "embedding": embedding,
            **(metadata or {}),
        }
        # pymilvus.Collection.insert 是同步接口;在 asyncio 环境下用 to_thread
        # 避免阻塞事件循环
        import asyncio

        await asyncio.to_thread(col.insert, [record])

    async def bulk_insert(
        self,
        records: list[tuple[str, list[float], dict[str, Any] | None]],
    ) -> dict[str, str]:
        """批量插入;Milvus ``Collection.insert([...])`` 单次 round-trip。

        :param records: ``[(mem_cell_id, embedding, metadata), ...]`` 列表;空
                        embedding 的条目会被本方法过滤(与单条 :meth:`insert`
                        语义一致)
        :returns: ``{失败的 mem_cell_id: 错误字符串}``;全部成功时为空 dict
        """
        rows = [
            (mid, emb, meta)
            for mid, emb, meta in records
            if emb  # 过滤空 embedding
        ]
        if not rows:
            return {}

        # 测试注入:逐条调 callable(保留对每条调用 mock 的能力)
        if self._insert_callable is not None:
            failures: dict[str, str] = {}
            for mid, emb, meta in rows:
                try:
                    await _maybe_await(
                        self._insert_callable(mid, emb, meta or {})
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("milvus bulk insert failed for %s: %s", mid, e)
                    failures[mid] = str(e)
            return failures

        # 真实路径:一次 Collection.insert 多条
        try:
            from pymilvus import Collection  # noqa: WPS433
        except ImportError as e:
            raise RuntimeError(
                "pymilvus not installed; provide insert_callable for tests"
            ) from e
        col = Collection(self.collection_name)
        batch = [
            {"mem_cell_id": mid, "embedding": emb, **(meta or {})}
            for mid, emb, meta in rows
        ]
        import asyncio

        try:
            await asyncio.to_thread(col.insert, batch)
            return {}
        except Exception as e:  # noqa: BLE001
            # Milvus insert 是 all-or-nothing;失败时全部回 DLQ
            err = str(e)
            logger.warning("milvus bulk insert failed (all %d): %s", len(rows), err)
            return {mid: err for mid, _, _ in rows}

    async def delete(self, mem_cell_id: str) -> None:
        """按主键删除向量。"""
        if self._delete_callable is not None:
            await _maybe_await(self._delete_callable(mem_cell_id))
            return
        try:
            from pymilvus import Collection  # noqa: WPS433
        except ImportError:
            return
        col = Collection(self.collection_name)
        import asyncio

        from memory_app.security.sanitize import escape_milvus_expr_string

        safe_id = escape_milvus_expr_string(mem_cell_id)
        await asyncio.to_thread(col.delete, f'mem_cell_id == "{safe_id}"')


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
async def _maybe_await(value):
    """若 value 是 awaitable 则 await,否则直接返回 —— 兼容同步 / 异步注入。"""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["MilvusMemCellRepo"]
