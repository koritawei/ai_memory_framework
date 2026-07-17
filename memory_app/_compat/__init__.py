"""``memory_app._compat`` —— 横切兼容工具集合。

子模块:
- :mod:`.time`        ``utcnow()`` —— tz-aware,替代弃用的 ``datetime.utcnow()``
- :mod:`.exceptions`  ``degraded()`` —— 统一"非关键路径失败仅 warn"的上下文
"""

from __future__ import annotations

from memory_app._compat.exceptions import degraded
from memory_app._compat.time import utcnow

__all__ = ["utcnow", "degraded"]
