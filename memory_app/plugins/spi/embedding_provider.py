"""EmbeddingProvider SPI —— 嵌入模型 Provider（设计文档 §13.1）。

默认实现 ``deepinfra_qwen3``（Qwen3-Embedding-4B，1024 维）；
可换 ``openai_text_embedding`` / ``local_bge`` / 测试 ``mock_embedding``。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.plugins.base import Plugin


class EmbeddingProvider(Plugin):
    """嵌入模型 Provider 扩展点。"""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """返回输出向量维度。

        约定：返回值在 Provider 生命周期内不变；不同 Provider 维度可能不同
        （1024 / 1536 / 768），上游 VectorStore 必须按此初始化 collection。
        """

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入。

        约定：
        - 返回列表长度严格等于 ``len(texts)``
        - 每个内层列表长度等于 :attr:`dimension`
        - 空字符串应被实现拒绝（抛 :class:`PluginError(category="config")`）
          或映射为 0 向量（实现自选，但需在 docstring 声明）
        - API 限流应由实现内部退避重试；连续 5 次失败抛 retryable=True 的
          :class:`PluginError`
        """


__all__ = ["EmbeddingProvider"]
