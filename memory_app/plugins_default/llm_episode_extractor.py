"""``llm_episode_extractor`` —— Phase 3 Step 3.2 情景抽取插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.episode_extractor.EpisodeExtractor` 的默认实现,
内部委托 :class:`memory_app.extractors.EpisodeMemoryExtractor` 的纯算法;
负责满足 Plugin 生命周期 + 注入 LLM client。

═══════════════════════════════════════════════════════════════════════════════
LLM client 注入
═══════════════════════════════════════════════════════════════════════════════
ConfigCenter ``params`` 不含 client 实例。生产装配:
``deps._init_cold_path_service`` 在 ``factory.build("memory.generation.episode_extractor")``
之后调 :meth:`bind_llm_client`。

测试装配:fixture 直接 ``LLMEpisodeExtractor()`` + ``await start({})`` +
``bind_llm_client(mock)``。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.extractors.episode_extractor import (
    DEFAULT_PROMPT_ID_BY_SCENARIO,
    EpisodeMemoryExtractor,
)
from memory_app.internal_models import EpisodicMemory, MemCell, SemanticMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.base import PluginError, PluginErrorCategory
from memory_app.plugins.spi.episode_extractor import (
    EpisodeExtractor,
    ScenarioType,
)

logger = logging.getLogger(__name__)


@register
class LLMEpisodeExtractor(EpisodeExtractor):
    """LLM 情景抽取(Phase 3 默认)。"""

    meta = PluginMeta(
        name="llm_episode_extractor",
        category="memory.generation.episode_extractor",
        version="1.0.0",
        description="LLM 情景抽取(prompt=episode_extraction[_group_chat])",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "prompt_id_assistant": {
                    "type": "string",
                    "default": "episode_extraction",
                },
                "prompt_id_group": {
                    "type": "string",
                    "default": "episode_extraction_group_chat",
                },
                "default_scenario": {
                    "type": "string",
                    "enum": ["assistant", "group_chat"],
                    "default": "group_chat",
                },
            },
        },
    )

    def __init__(self) -> None:
        self._llm_client: Any = None
        self._core: EpisodeMemoryExtractor | None = None
        self._prompt_id_assistant: str = "episode_extraction"
        self._prompt_id_group: str = "episode_extraction_group_chat"
        self._default_scenario: ScenarioType = ScenarioType.GROUP_CHAT

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._prompt_id_assistant = str(config.get("prompt_id_assistant", "episode_extraction"))
        self._prompt_id_group = str(config.get("prompt_id_group", "episode_extraction_group_chat"))
        scen = str(config.get("default_scenario", "group_chat")).lower()
        self._default_scenario = (
            ScenarioType.ASSISTANT if scen == "assistant" else ScenarioType.GROUP_CHAT
        )
        # core 在 bind_llm_client 后真正可用;先构造空壳便于幂等
        self._rebuild_core()
        logger.info(
            "llm_episode_extractor started: prompts=%s/%s, default=%s",
            self._prompt_id_assistant, self._prompt_id_group, self._default_scenario.value,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok" if self._llm_client is not None else "degraded",
            "detail": (
                f"client={'bound' if self._llm_client is not None else 'unbound'}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # client 注入
    # ────────────────────────────────────────────────────────────────────────
    def bind_llm_client(self, client: Any) -> None:
        """绑定 LLMProvider 鸭子类型对象(``await client.generate(prompt) -> str``)。"""
        self._llm_client = client
        self._rebuild_core()

    def _rebuild_core(self) -> None:
        # 即便 client 为 None 也构造,保证 core 字段存在(extract 时再校验)
        self._core = EpisodeMemoryExtractor(
            llm_client=self._llm_client,
            prompt_id_assistant=self._prompt_id_assistant,
            prompt_id_group=self._prompt_id_group,
            default_scenario=self._default_scenario,
        )

    # ────────────────────────────────────────────────────────────────────────
    # SPI: extract
    # ────────────────────────────────────────────────────────────────────────
    async def extract(
        self,
        memcell: MemCell,
        old_memories: list[SemanticMemory] | None = None,  # noqa: ARG002 (Phase 4+ 用得上)
        scenario: ScenarioType | None = None,
    ) -> list[EpisodicMemory]:
        """SPI 契约:从 MemCell 抽 EpisodicMemory 列表。

        - 调用方未显式指定 ``scenario``(``None``)→ 用 ConfigCenter 下发的 ``default_scenario``
        - LLM 未绑定 → :class:`PluginError(category="dependency", retryable=True)`
        - LLM 调用异常 → 包装为同上
        """
        if self._core is None or self._llm_client is None:
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "llm_client_unbound",
                "llm_episode_extractor: llm_client not bound",
                retryable=True,
            )
        effective_scenario = scenario if scenario is not None else self._default_scenario
        try:
            return await self._core.extract(memcell, scenario=effective_scenario)
        except PluginError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PluginError(
                PluginErrorCategory.DEPENDENCY,
                "llm_extract_failed",
                f"episode extract failed: {e}",
                retryable=True,
                cause=e,
            ) from e


__all__ = ["LLMEpisodeExtractor", "DEFAULT_PROMPT_ID_BY_SCENARIO"]
