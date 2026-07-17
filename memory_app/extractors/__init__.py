"""核心抽取器(Phase 3 冷路径)。

═══════════════════════════════════════════════════════════════════════════════
角色定位
═══════════════════════════════════════════════════════════════════════════════
本目录承载 "纯算法 / 无外部连接" 的 LLM 抽取核心类。
插件层 :mod:`memory_app.plugins_default.llm_episode_extractor` /
:mod:`memory_app.plugins_default.llm_10_association` 是这些核心类的薄包装,
负责满足 SPI 契约 + 接入 :class:`PluginFactory`。

═══════════════════════════════════════════════════════════════════════════════
为什么核心算法不直接写在插件文件里
═══════════════════════════════════════════════════════════════════════════════
- 评测脚本 / CLI 工具可绕过 PluginFactory 直接 ``from memory_app.extractors import ...``
- 单元测试可独立于 ConfigCenter / Registry 建立测试装配
- 与 ``memory_app/sbd.py`` 同模式(纯函数 / 算法 + 插件薄包装)
"""

from memory_app.extractors.episode_extractor import (
    EpisodeMemoryExtractor,
    parse_episode_response,
)
from memory_app.extractors.semantic_extractor import (
    SemanticMemoryExtractor,
    parse_semantic_response,
)

__all__ = [
    "EpisodeMemoryExtractor",
    "parse_episode_response",
    "SemanticMemoryExtractor",
    "parse_semantic_response",
]
