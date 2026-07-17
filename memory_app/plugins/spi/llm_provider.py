"""LLMProvider SPI —— 大模型 Provider。

默认实现 ``anthropic`` / ``openai``；测试用 ``mock_llm``。
"""

from __future__ import annotations

from abc import abstractmethod

from memory_app.plugins.base import Plugin


class LLMProvider(Plugin):
    """LLM Provider 扩展点。"""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        json_schema: dict | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> str:
        """生成文本。

        约定：
        - 返回值是 LLM 原始字符串（**不**做 JSON 解析 —— 让调用方自己做容错）
        - ``json_schema`` 非空时实现可选启用 OpenAI Structured Outputs / Anthropic
          tool-use 强约束输出；不支持的实现可忽略此参数（调用方需做 4 级 JSON
          回退解析，见 §5.1.3.8 EventLogExtractor）
        - ``temperature=0.0`` 表示尽量确定性输出（事实抽取 / 价值判别场景）
        - 限流 / 重试由实现内部处理；不可恢复错误抛
          :class:`PluginError(category="dependency", retryable=False)`
        """


__all__ = ["LLMProvider"]
