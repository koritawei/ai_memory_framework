"""tz-aware ``utcnow()`` —— 替代 Python 3.12+ 弃用的 ``datetime.utcnow()``。

═══════════════════════════════════════════════════════════════════════════════
背景
═══════════════════════════════════════════════════════════════════════════════
``datetime.utcnow()`` 自 Python 3.12 起 DeprecationWarning,因其返回**naive**
datetime,与 ``datetime.now(timezone.utc)`` 返回的 tz-aware 实例混用会引发
微妙 bug(``replace(tzinfo=...)`` 后又被去掉等)。

约定:全工程**只**通过本函数取 UTC 时间。Pydantic 模型的
``Field(default_factory=...)`` 也应传本函数,避免 default_factory 触发
弃用警告。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """返回当前 UTC 时刻(tz-aware)。"""
    return datetime.now(timezone.utc)


__all__ = ["utcnow"]
