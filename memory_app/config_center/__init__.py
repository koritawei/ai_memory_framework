"""统一配置中心(设计文档 §2.8)。

═══════════════════════════════════════════════════════════════════════════════
类层级(A + B 嵌套,§2.8.6.1)
═══════════════════════════════════════════════════════════════════════════════

::

    ConfigCenter(ABC,base.py)
    └── BaseConfigCenter(通用流程骨架,_common.py;混入 PromptConfigMixin)
        ├── FileConfigCenter(YAML 文件,file_center.py)
        └── DBConfigCenter(关系/文档 DB 共享层,_db.py)
            ├── MongoConfigCenter(motor 适配,mongo_center.py)
            └── (未来:PGConfigCenter / SQLiteConfigCenter)

判定原则(§2.8.6.1)
─────────────────────────────────────────────────────────────────────────────
- 后端持久化模型是「行/文档 + CRUD」 → 继承 :class:`DBConfigCenter`
- 后端持久化模型不是 CRUD 范式(KV 树 / 远程 HTTP / 文件 mtime)→ 直接继承
  :class:`BaseConfigCenter`,自行实现 4 个 hook

公共契约
─────────────────────────────────────────────────────────────────────────────
- :class:`ConfigCenter`           接口契约
- :class:`ResolvedPluginConfig`   plugin resolve 返回值
- :class:`ResolvedPromptConfig`   prompt resolve 返回值(§2.8.4.1)
- :class:`ConfigChangeEvent`      变更事件载荷
- :class:`ConfigValidationError`  Schema 校验失败异常
- :class:`ConfigResolver`         五级覆盖 + 5 维灰度匹配引擎
- :class:`PromptConfigMixin`      Prompt 解析/写入/历史能力(混入 BaseConfigCenter)
- :class:`PromptNotFoundError`    prompt_id 未命中异常
"""

from memory_app.prompt_manager.models import ResolvedPromptConfig

from ._common import BaseConfigCenter
from ._db import DBConfigCenter
from ._prompts import PromptConfigMixin, PromptNotFoundError
from .base import (
    ConfigCenter,
    ConfigChangeCallback,
    ConfigChangeEvent,
    ConfigValidationError,
    ResolvedPluginConfig,
)
from .file_center import FileConfigCenter
from .mongo_center import MongoConfigCenter
from .prompt_paths import (
    PROMPT_CATEGORY_PREFIX,
    is_prompt_category,
    parse_prompt_id,
    prompt_category,
)
from .prompt_schema import validate_prompt_body
from .resolver import ConfigResolver

__all__ = [
    # 类层级
    "ConfigCenter",
    "BaseConfigCenter",
    "DBConfigCenter",
    "FileConfigCenter",
    "MongoConfigCenter",
    # 异常
    "ConfigValidationError",
    "PromptNotFoundError",
    # 事件
    "ConfigChangeEvent",
    "ConfigChangeCallback",
    # plugin / prompt 解析结果
    "ResolvedPluginConfig",
    "ResolvedPromptConfig",
    # 引擎
    "ConfigResolver",
    # Prompt 工具
    "PromptConfigMixin",
    "PROMPT_CATEGORY_PREFIX",
    "prompt_category",
    "parse_prompt_id",
    "is_prompt_category",
    "validate_prompt_body",
]
