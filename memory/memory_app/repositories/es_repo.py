"""MemCell 在 Elasticsearch 的索引层。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
ES 承载 BM25 全文检索;检索 起被 ``BM25Channel`` 用作多路召回之一。

文档结构(写入热路径 最小集合)
─────────────────────────────────────────────────────────────────────────────
``mem_cell_id``      doc id,与 ``MemCell.mem_cell_id`` 一致
``tenant_id``        租户隔离(过滤索引)
``user_id``          用户隔离
``session_id``       会话标识
``text``             检索主体字段(BM25 主分项)
``state``            生命周期状态(``ACTIVE`` / ``COLD`` / ...)
``created_at``       ISO8601 时间戳

═══════════════════════════════════════════════════════════════════════════════
索引名约定
═══════════════════════════════════════════════════════════════════════════════
``{prefix}_mem_cells`` 形态;``prefix`` 来自 :class:`Settings.es_index_prefix`。
不同环境(dev / staging / prod)用同一段代码 + 不同 prefix 隔离。
"""

from __future__ import annotations

import logging
from typing import Any

from memory_app.internal_models import MemCell

logger = logging.getLogger(__name__)


class ESMemCellRepo:
    """Elasticsearch MemCell 索引仓储。

    构造接收 ``elasticsearch.AsyncElasticsearch`` 实例;为单元测试便利,
    类型注解保持宽松(支持 fake ES 客户端)。
    """

    def __init__(
        self,
        es_client: Any,
        index_prefix: str = "memory",
        index_suffix: str = "mem_cells",
    ) -> None:
        self._es = es_client
        self.index_name = f"{index_prefix}_{index_suffix}"

    # ════════════════════════════════════════════════════════════════════════
    # 索引建立(幂等)
    # ════════════════════════════════════════════════════════════════════════
    async def ensure_index(self) -> None:
        """启动期幂等创建索引;失败仅 warn。

        写入热路径 用最小映射;检索 起按需扩展(``analyzer`` / 多字段等)。
        """
        try:
            exists = await self._es.indices.exists(index=self.index_name)
            # AsyncElasticsearch 返回 ObjectApiResponse;``bool`` 转换走 200/404
            if not bool(exists):
                await self._es.indices.create(
                    index=self.index_name,
                    mappings={
                        "properties": {
                            "mem_cell_id": {"type": "keyword"},
                            "tenant_id": {"type": "keyword"},
                            "user_id": {"type": "keyword"},
                            "session_id": {"type": "keyword"},
                            "state": {"type": "keyword"},
                            "text": {"type": "text"},
                            "created_at": {"type": "date"},
                        }
                    },
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("ensure_index(es) failed (degraded): %s", e)

    # ════════════════════════════════════════════════════════════════════════
    # 写入
    # ════════════════════════════════════════════════════════════════════════
    async def index(self, cell: MemCell) -> None:
        """对单条 MemCell 做 ES 索引(upsert 语义)。

        失败抛原异常;:class:`IngestService` 据此决定是否走 DLQ 降级。
        """
        doc = self._to_doc(cell)
        await self._es.index(
            index=self.index_name, id=cell.mem_cell_id, document=doc
        )

    async def bulk_index(self, cells: list[MemCell]) -> dict[str, str]:
        """批量索引;一次 ES Bulk API 调用完成所有写入。

        替代 ``for c in cells: await self.index(c)`` 的 N 次 HTTP round-trip。

        ES Bulk 响应每条都有独立 ok / error,本方法把失败的 ``mem_cell_id`` 与
        其原始错误字符串一起返回,调用方(``SyncIndexStage``)据此精确填 DLQ。

        兼容性:client 不支持 ``bulk`` API(测试 fake / 旧 stub)时**透明降级**
        到 per-cell ``index`` 串行,保留每条原始异常信息。

        :returns: ``{失败的 mem_cell_id: 错误字符串}``;全部成功时为空 dict
        """
        if not cells:
            return {}
        if not hasattr(self._es, "bulk"):
            # 兼容降级:client 没有 bulk 接口 → per-cell
            failures: dict[str, str] = {}
            for cell in cells:
                try:
                    await self.index(cell)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "ES per-cell fallback failed for %s: %s",
                        cell.mem_cell_id, e,
                    )
                    failures[cell.mem_cell_id] = str(e)
            return failures

        # ES Bulk body: 每条 doc 前面一个 action header
        body: list[dict] = []
        for cell in cells:
            body.append({"index": {"_index": self.index_name, "_id": cell.mem_cell_id}})
            body.append(self._to_doc(cell))
        # AsyncElasticsearch.bulk 接受 operations 参数(v8.x);旧版本接受 body
        try:
            resp = await self._es.bulk(operations=body)
        except TypeError:
            resp = await self._es.bulk(body=body)
        # 解析失败项
        failures = {}
        items = resp.get("items") if isinstance(resp, dict) else getattr(resp, "body", {}).get("items", [])
        if items:
            for entry in items:
                op = entry.get("index") or entry.get("create") or {}
                err = op.get("error")
                if err:
                    mid = str(op.get("_id", ""))
                    err_msg = err.get("reason") if isinstance(err, dict) else str(err)
                    failures[mid] = err_msg or "bulk_index entry failed"
        return failures

    async def delete(self, mem_cell_id: str) -> None:
        """按 doc id 删除(若不存在,ignore 404)。"""
        try:
            await self._es.delete(
                index=self.index_name, id=mem_cell_id, ignore=[404]
            )
        except TypeError:
            # 部分客户端 stub 不支持 ignore 参数;退化为吞 NotFound
            try:
                await self._es.delete(index=self.index_name, id=mem_cell_id)
            except Exception:  # noqa: BLE001
                pass

    # ════════════════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _to_doc(cell: MemCell) -> dict[str, Any]:
        return {
            "mem_cell_id": cell.mem_cell_id,
            "tenant_id": cell.tenant_id,
            "user_id": cell.user_id,
            "session_id": cell.session_id,
            "state": cell.state.value,
            "text": cell.text,
            "created_at": cell.created_at.isoformat(),
        }


__all__ = ["ESMemCellRepo"]
