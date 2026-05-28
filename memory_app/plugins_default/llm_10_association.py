"""``llm_10_association`` —— 冷路径 语义抽取插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.semantic_extractor.SemanticExtractor` 的默认实现,
内部委托 :class:`memory_app.extractors.SemanticMemoryExtractor`。

名字 "10_association" 来自 "10 联想策略" —— LLM 应产出约 10 条
语义记忆;实际产出条数由 ``min_items`` / ``max_items`` 限制(默认 [0, 20])。

═══════════════════════════════════════════════════════════════════════════════
LLM client 注入
═══════════════════════════════════════════════════════════════════════════════
同 ``llm_episode_extractor``:ConfigCenter 启动期 ``params`` 不含 client,
由装配代码 / 测试 fixture 后续 :meth:`bind_llm_client` 注入。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.extractors.semantic_extractor import SemanticMemoryExtractor
from memory_app.internal_models import EpisodicMemory, MemCell, SemanticMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.semantic_extractor import SemanticExtractor

logger = logging.getLogger(__name__)


@register
class LLM10AssociationExtractor(SemanticExtractor):
    """LLM "10 联想" 语义抽取插件。"""

    meta = PluginMeta(
        name="llm_10_association",
        category="memory.generation.semantic_extractor",
        version="1.0.0",
        description="LLM 语义联想(prompt=semantic_extraction;~10 条/次)",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "prompt_id": {"type": "string", "default": "semantic_extraction"},
                "min_items": {"type": "integer", "minimum": 0, "maximum": 50, "default": 0},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    )

    def __init__(self) -> None:
        self._llm_client: Any = None
        self._core: SemanticMemoryExtractor | None = None
        self._prompt_id: str = "semantic_extraction"
        self._min_items: int = 0
        self._max_items: int = 20

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._prompt_id = str(config.get("prompt_id", "semantic_extraction"))
        self._min_items = int(config.get("min_items", 0))
        self._max_items = int(config.get("max_items", 20))
        self._rebuild_core()
        logger.info(
            "llm_10_association started: prompt=%s, items=[%d, %d]",
            self._prompt_id, self._min_items, self._max_items,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok" if self._llm_client is not None else "degraded",
            "detail": (
                f"client={'bound' if self._llm_client is not None else 'unbound'}, "
                f"prompt={self._prompt_id}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # client 注入
    # ────────────────────────────────────────────────────────────────────────
    def bind_llm_client(self, client: Any) -> None:
        self._llm_client = client
        self._rebuild_core()

    def _rebuild_core(self) -> None:
        self._core = SemanticMemoryExtractor(
            llm_client=self._llm_client,
            prompt_id=self._prompt_id,
            min_items=self._min_items,
            max_items=self._max_items,
        )

    # ────────────────────────────────────────────────────────────────────────
    # SPI: extract_for_episode / extract_for_memcell
    # ────────────────────────────────────────────────────────────────────────
    async def extract_for_episode(self, episode: EpisodicMemory) -> list[SemanticMemory]:
        self._ensure_ready()
        try:
            return await self._core.extract(episode)  # type: ignore[union-attr]
        except PluginError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "llm_extract_failed",
                f"semantic extract failed: {e}",
                retryable=True,
                cause=e,
            ) from e

    async def extract_for_memcell(self, memcell: MemCell) -> list[SemanticMemory]:
        self._ensure_ready()
        try:
            return await self._core.extract_for_memcell(memcell)  # type: ignore[union-attr]
        except PluginError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "llm_extract_failed",
                f"semantic extract_for_memcell failed: {e}",
                retryable=True,
                cause=e,
            ) from e

    # ────────────────────────────────────────────────────────────────────────
    def _ensure_ready(self) -> None:
        if self._core is None or self._llm_client is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "llm_client_unbound",
                "llm_10_association: llm_client not bound",
                retryable=True,
            )


__all__ = ["LLM10AssociationExtractor"]
