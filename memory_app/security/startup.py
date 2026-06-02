"""启动期安全配置校验。"""

from __future__ import annotations

import logging

from memory_app.settings import Settings

logger = logging.getLogger(__name__)


def validate_startup_security(settings: Settings) -> None:
    """生产态（``debug=false``）启动安全检查。

    - ``auth_enabled=false`` → 拒绝启动（业务面完全开放）
    - 未配置 ``admin_api_key`` → 严重告警（管理面可能裸露）
    """
    if settings.debug:
        return
    if not settings.auth_enabled:
        msg = (
            "refusing to start: auth_enabled=false with debug=false. "
            "Set MEMORY_AUTH_ENABLED=true or MEMORY_DEBUG=true for local dev."
        )
        logger.critical(msg)
        raise RuntimeError(msg)
    if not settings.admin_api_key:
        msg = (
            "refusing to start: admin_api_key is not configured with debug=false. "
            "Set MEMORY_ADMIN_API_KEY or MEMORY_DEBUG=true for local dev."
        )
        logger.critical(msg)
        raise RuntimeError(msg)


__all__ = ["validate_startup_security"]
