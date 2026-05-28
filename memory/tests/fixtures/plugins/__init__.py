"""Mock 插件集合(冷路径 起 CI 全靠这些避开付费 API)。

import 本包即触发 ``@register`` 装饰器,注册到全局 registry。
测试用例可在 conftest 中显式 ``import tests.fixtures.plugins  # noqa`` 触发。
"""

# 触发 @register
from . import mock_embedding, mock_llm  # noqa: F401

__all__: list[str] = []
