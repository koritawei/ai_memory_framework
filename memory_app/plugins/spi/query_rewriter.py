"""QueryRewriter SPI —— 查询改写（设计文档 §12.4 多轮检索）。

Phase 1 默认 ``identity_rewriter``（直接返回原查询）；
Phase 2+ 切到 ``multi_query_rewriter`` 做 Agentic 多查询展开（HyDE / Multi-Query）。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.plugins.base import Plugin


class QueryRewriter(Plugin):
    """查询改写扩展点。"""

    @abstractmethod
    async def rewrite(self, query: str, intent: str | None = None) -> list[str]:
        """把单查询改写为一组等价/相关查询。

        约定：
        - 返回列表至少含 1 条（identity 模式即返回 ``[query]``）
        - 多查询场景一般返回 3 条（参考 §12.4 Agentic 检索 ``num_queries=3``）
        - 改写不应改变原查询的意图大类
        - LLM 失败应抛 :class:`PluginError(retryable=True)` 让上游降级到 identity
        """


__all__ = ["QueryRewriter"]
