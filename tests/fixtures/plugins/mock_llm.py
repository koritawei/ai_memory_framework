"""``mock_llm`` —— 测试用 LLMProvider(不调付费 API)。

═══════════════════════════════════════════════════════════════════════════════
行为
═══════════════════════════════════════════════════════════════════════════════
- 默认实现 :meth:`generate` 返回构造时注入的 ``responses`` 队列首元素
- 队列为空 → 返回 ``default_response``
- 队列被消费完后 ``calls`` 属性可供测试断言

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    responses:        list[str] LLM 顺序响应队列(测试可直接绑定)
    default_response: str       队列耗尽时的默认值(默认 "[]")

ConfigCenter ``params`` 不含上述运行时数据;通过
:func:`set_mock_responses` 在测试 fixture 中显式注入。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Mapping

from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.llm_provider import LLMProvider

logger = logging.getLogger(__name__)


@register
class MockLLMProvider(LLMProvider):
    """测试用 LLMProvider —— 按队列吐字符串。"""

    meta = PluginMeta(
        name="mock_llm",
        category="memory.provider.llm",
        version="1.0.0",
        description="测试 mock(不调付费 API);队列耗尽时返回 default_response",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "default_response": {"type": "string", "default": "[]"},
            },
        },
    )

    def __init__(self) -> None:
        self._responses: deque[str] = deque()
        self._default: str = "[]"
        self.calls: list[str] = []  # 测试可读

    async def start(self, config: Mapping[str, Any]) -> None:
        self._default = str(config.get("default_response", "[]"))
        # responses 来自显式 set_responses;config 不接收(避免 schema 复杂)
        logger.info("mock_llm started: default=%r", self._default)

    async def stop(self) -> None:
        self._responses.clear()

    def set_responses(self, responses: list[str]) -> None:
        """测试入口:设置 LLM 响应顺序队列。"""
        self._responses = deque(responses)

    async def generate(
        self,
        prompt: str,
        json_schema: dict | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> str:
        self.calls.append(prompt)
        if self._responses:
            return self._responses.popleft()
        return self._default


__all__ = ["MockLLMProvider"]
