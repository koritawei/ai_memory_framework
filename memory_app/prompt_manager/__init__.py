"""LLM Prompt 运行时。

═══════════════════════════════════════════════════════════════════════════════
出口
═══════════════════════════════════════════════════════════════════════════════
- :class:`PromptSpec`                Prompt 静态规格(写入 ConfigCenter 的 body)
- :class:`ResolvedPromptConfig`      解析结果(含 source 标签)
- :class:`StandalonePromptManager`   单测/无 ConfigCenter 场景的最小实现
- :class:`ConfigCenterPromptManager` 运行时默认实现(读 ConfigCenter + watch)
- :data:`BUILTIN_PROMPTS`            内置种子(运维删除 default.yaml 后的兜底)

业务侧 import 仅用 :mod:`memory_app.prompt_runtime` 中的 ``get_prompt_manager``,
不直接 import 本子包以避免在 冷路径 提取器内部硬连接具体类。
"""

from .builtins import BUILTIN_PROMPTS
from .config_backed import ConfigCenterPromptManager
from .manager import StandalonePromptManager
from .models import PromptSpec, ResolvedPromptConfig

__all__ = [
    "PromptSpec",
    "ResolvedPromptConfig",
    "StandalonePromptManager",
    "ConfigCenterPromptManager",
    "BUILTIN_PROMPTS",
]
