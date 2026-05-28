"""``composite`` —— 离线巩固 默认 Consolidator 插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.consolidator.Consolidator` 的默认实现。
内部委托 :class:`memory_app.consolidator.Consolidator` 算法,薄包装。

 提到 composite 实际包含三层:
1. 规则(Jaccard + Cosine) → 本插件 核心实现 实现
2. Sheaf Cohomology      → 冷路径+ 启用(配置 ``enable_sheaf=true``)
3. LLM 兜底              → 冷路径+ 启用(配置 ``enable_llm_fallback=true``)

离线巩固 仅落第 1 层;后两层留 hook 不强依赖。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.consolidator import (
    Consolidator as CoreConsolidator,
    parse_consolidate_config,
)
from memory_app.internal_models import SemanticMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.consolidator import (
    Consolidator,
    ConsolidatorResult,
)

logger = logging.getLogger(__name__)


@register
class CompositeConsolidator(Consolidator):
    """Composite Consolidator(离线巩固 默认)。"""

    meta = PluginMeta(
        name="composite",
        category="memory.lifecycle.consolidator",
        version="1.0.0",
        description="Jaccard + Cosine 综合相似度;核心实现 不含 Sheaf / LLM 兜底",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "update_threshold": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.85,
                },
                "supersede_threshold": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.93,
                },
                "noop_threshold": {
                    "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.97,
                },
                "w_jaccard": {"type": "number", "minimum": 0.0, "default": 0.4},
                "w_cosine": {"type": "number", "minimum": 0.0, "default": 0.6},
                "enable_sheaf": {"type": "boolean", "default": False},
                "enable_llm_fallback": {"type": "boolean", "default": False},
            },
        },
    )

    def __init__(self) -> None:
        self._embedding_client: Any = None
        self._core: CoreConsolidator = CoreConsolidator()
        self._enable_sheaf: bool = False
        self._enable_llm_fallback: bool = False

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        cfg = parse_consolidate_config(dict(config))
        self._enable_sheaf = bool(config.get("enable_sheaf", False))
        self._enable_llm_fallback = bool(config.get("enable_llm_fallback", False))
        self._core = CoreConsolidator(
            config=cfg, embedding_client=self._embedding_client
        )
        logger.info(
            "composite consolidator started: update>=%.2f, supersede>=%.2f, noop>=%.2f",
            cfg.update_threshold, cfg.supersede_threshold, cfg.noop_threshold,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"update={self._core.config.update_threshold}, "
                f"supersede={self._core.config.supersede_threshold}, "
                f"noop={self._core.config.noop_threshold}, "
                f"sheaf={self._enable_sheaf}, llm={self._enable_llm_fallback}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # client 注入(EmbeddingProvider)
    # ────────────────────────────────────────────────────────────────────────
    def bind_embedding_client(self, client: Any) -> None:
        self._embedding_client = client
        # 重建 core 让其拿到新 client
        self._core = CoreConsolidator(
            config=self._core.config, embedding_client=client
        )

    # ────────────────────────────────────────────────────────────────────────
    # SPI: consolidate
    # ────────────────────────────────────────────────────────────────────────
    async def consolidate(
        self,
        new_fact: SemanticMemory,
        existing_facts: list[SemanticMemory],
    ) -> ConsolidatorResult:
        return await self._core.consolidate(new_fact, existing_facts)


__all__ = ["CompositeConsolidator"]
