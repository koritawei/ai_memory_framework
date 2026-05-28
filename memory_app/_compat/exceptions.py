"""``degraded`` —— 统一"非关键路径失败仅 warn"上下文管理器。

═══════════════════════════════════════════════════════════════════════════════
背景
═══════════════════════════════════════════════════════════════════════════════
工程中有 100+ 处 ``try / except Exception / logger.warning(...)`` 样板,
散落在装配层(各 ServiceBuilder)、close 路径、可选子组件初始化等位置。
本工具把它收口为单一上下文,统一日志格式 + 便于全局抓取:

.. code-block:: python

    from memory_app._compat import degraded

    with degraded("init mongo"):
        client = AsyncIOMotorClient(...)

    # 异常时日志: "degraded: init mongo failed: <reason>"

═══════════════════════════════════════════════════════════════════════════════
何时**不**用
═══════════════════════════════════════════════════════════════════════════════
- 必启项(ConfigCenter / PluginRegistry):应直接抛
- 后台任务的领域错误:走 BackgroundTaskRunner 的 retry + DLQ
- 用户输入解析:抛 :class:`UsageError`(收窄到 ``json.JSONDecodeError`` /
  ``OSError`` 等具体异常)
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def degraded(
    action: str,
    *,
    logger: logging.Logger | None = None,
    reraise: tuple[type[BaseException], ...] = (),
) -> Iterator[None]:
    """非关键路径失败仅 warn 不抛。

    :param action:    人类可读的动作描述,出现在日志中
    :param logger:    日志器(默认用 ``logging.getLogger(__name__)``)
    :param reraise:   仍需上抛的异常类元组(如 ``(KeyboardInterrupt,)`` 防止吞 SIGINT)

    用法:

    .. code-block:: python

        with degraded("init redis"):
            self.redis = Redis.from_url(...)
    """
    log = logger or logging.getLogger("memory_app._compat.degraded")
    try:
        yield
    except reraise:  # type: ignore[misc]
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("degraded: %s failed: %s", action, e)


__all__ = ["degraded"]
