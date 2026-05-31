"""CLI 异常体系 + 退出码常量。

═══════════════════════════════════════════════════════════════════════════════
退出码契约(与原 ``cli.py`` 一致)
═══════════════════════════════════════════════════════════════════════════════
- 0  成功
- 2  参数错误(argparse / 用户输入校验)
- 3  业务错误(HTTP 4xx/5xx 或本地命令报错)
- 4  服务不可达(HTTP 连接异常)
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BUSINESS = 3
EXIT_UNREACHABLE = 4


class CliError(Exception):
    """CLI 终端异常 —— 替代旧版 ``_die()`` 中 ``sys.exit`` 直接退出的反模式。

    main 函数捕获本异常,统一打印到 stderr 并返回 ``exit_code``,便于单元测试
    断言"哪种输入导致哪个退出码"。
    """

    def __init__(self, message: str, exit_code: int = EXIT_BUSINESS) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class UsageError(CliError):
    """用户输入或参数解析错误 → 退出码 2。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_USAGE)


class BusinessError(CliError):
    """业务错误(HTTP 非 2xx 等)→ 退出码 3。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_BUSINESS)


class UnreachableError(CliError):
    """服务不可达 → 退出码 4。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_UNREACHABLE)


__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_BUSINESS",
    "EXIT_UNREACHABLE",
    "CliError",
    "UsageError",
    "BusinessError",
    "UnreachableError",
]
