"""BaseRetrievalChannel —— 单路召回模板方法(设计文档 §6.1)。

═══════════════════════════════════════════════════════════════════════════════
模板方法骨架
═══════════════════════════════════════════════════════════════════════════════
::

    search(tenant_id, user_id, query, top_k)
        ├── _check_dependencies()   子类实现:依赖客户端是否注入
        ├── _execute_search(...)    子类实现:调底层 ES/Milvus
        ├── _parse_hits(raw)        子类实现:把底层响应转 RankedMemory 列表
        └── _sort_hits(hits)        基类实现:按 score 降序 + 填 rank

子类只覆写抽象的三步,不写循环 / 排序 / 防御 —— 一致性由本类保证。

═══════════════════════════════════════════════════════════════════════════════
失败语义(SPI 契约)
═══════════════════════════════════════════════════════════════════════════════
- 客户端不可达 → :class:`PluginError(category="dependency", retryable=True)`
- 真无结果(底层正常返回空)→ 返回**空列表**,**不**抛
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from memory_app.internal_models import RankedMemory
from memory_app.plugins.base import PluginError, PluginErrorCategory

logger = logging.getLogger(__name__)


class BaseRetrievalChannel(ABC):
    """单路召回通道的纯算法基类(无 SPI 包袱)。

    SPI 实现 :class:`memory_app.plugins.spi.retrieval_channel.RetrievalChannel`
    经插件层(``plugins_default/bm25_es_channel.py`` 等)委托本类。
    """

    #: 子类应覆盖,返回固定字符串(如 ``"bm25"`` / ``"vector"``)
    channel_name: str = "base"

    # ────────────────────────────────────────────────────────────────────────
    # 模板方法
    # ────────────────────────────────────────────────────────────────────────
    async def search(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int = 10,
        *,
        filters: dict[str, Any] | None = None,
    ) -> list[RankedMemory]:
        """执行召回,统一管控依赖检查 / 错误包装 / 排序。

        :raises PluginError: 客户端缺失或底层 dependency 失败
        """
        self._check_dependencies()
        if not query or not query.strip():
            return []
        if top_k <= 0:
            return []
        try:
            raw = await self._execute_search(
                tenant_id=tenant_id,
                user_id=user_id,
                query=query,
                top_k=top_k,
                filters=filters or {},
            )
        except PluginError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                f"{self.channel_name}_failed",
                f"{self.channel_name} channel failed: {e}",
                retryable=True,
                cause=e,
            ) from e
        hits = self._parse_hits(raw)
        return self._sort_hits(hits)

    # ────────────────────────────────────────────────────────────────────────
    # 子类抽象
    # ────────────────────────────────────────────────────────────────────────
    @abstractmethod
    def _check_dependencies(self) -> None:
        """检查客户端是否注入。失败应抛
        :class:`PluginError(category="dependency", retryable=True)`。
        """

    @abstractmethod
    async def _execute_search(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int,
        filters: dict[str, Any],
    ) -> Any:
        """调用底层客户端获取原始响应。返回结构由子类定义。"""

    @abstractmethod
    def _parse_hits(self, raw: Any) -> list[RankedMemory]:
        """把底层响应解析为 :class:`RankedMemory` 列表。

        约定:
        - ``RankedMemory.source_channel`` 必须等于 :attr:`channel_name`
        - ``score`` 由子类填(BM25 直接用 ES _score;Vector 用 distance)
        """

    # ────────────────────────────────────────────────────────────────────────
    # 基类实现:排序 + rank
    # ────────────────────────────────────────────────────────────────────────
    def _sort_hits(self, hits: list[RankedMemory]) -> list[RankedMemory]:
        """按 ``score`` 降序排,从 0 开始填 ``rank``。"""
        sorted_hits = sorted(hits, key=lambda h: h.score, reverse=True)
        for i, h in enumerate(sorted_hits):
            h.rank = i
            if not h.source_channel:
                h.source_channel = self.channel_name
        return sorted_hits


__all__ = ["BaseRetrievalChannel"]
