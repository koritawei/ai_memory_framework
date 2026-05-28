"""``mock_embedding`` —— 测试用 EmbeddingProvider(零付费,确定性输出)。

═══════════════════════════════════════════════════════════════════════════════
行为
═══════════════════════════════════════════════════════════════════════════════
默认 ``dimension=8``,使用 :func:`hashlib.sha1` 把文本映射为浮点列表 —— 确定
但**非语义**;仅供管线 / 聚类的"接线测试"用,**不**适合做检索质量评测。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping

from memory_app.plugins import PluginMeta, register
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.embedding_provider import EmbeddingProvider

logger = logging.getLogger(__name__)


@register
class MockEmbeddingProvider(EmbeddingProvider):
    """确定性 mock embedding(8 维,SHA1 派生)。"""

    meta = PluginMeta(
        name="mock_embedding",
        category="memory.provider.embedding",
        version="1.0.0",
        description="测试 mock(SHA1 派生 8 维向量);非语义,仅供接线",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "dimension": {"type": "integer", "minimum": 4, "maximum": 64, "default": 8},
            },
        },
    )

    def __init__(self) -> None:
        self._dimension: int = 8

    @property
    def dimension(self) -> int:
        return self._dimension

    async def start(self, config: Mapping[str, Any]) -> None:
        self._dimension = int(config.get("dimension", 8))

    async def stop(self) -> None:
        return None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            if not text:
                raise PluginError(
                    PluginErrorCategory.CONFIG,
                    "empty_text",
                    "mock_embedding: empty text not supported",
                )
            out.append(_hash_embed(text, self._dimension))
        return out


# ════════════════════════════════════════════════════════════════════════════
# 内部
# ════════════════════════════════════════════════════════════════════════════
def _hash_embed(text: str, dim: int) -> list[float]:
    """SHA1 → bytes → 4 字节切片 → 归一化到 [-1, 1]。"""
    digest = hashlib.sha1(text.encode("utf-8")).digest()
    out: list[float] = []
    for i in range(dim):
        chunk = digest[(i * 4) % len(digest) : (i * 4) % len(digest) + 4]
        if len(chunk) < 4:
            chunk = (chunk + digest)[:4]
        val = int.from_bytes(chunk, "big", signed=False)
        # 归一到 [-1, 1]
        out.append((val / 0x7FFF_FFFF) - 1.0)
    return out


__all__ = ["MockEmbeddingProvider"]
