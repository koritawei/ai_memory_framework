"""FastAPI 路由集合（设计文档 §2.5.1）。

Phase 0 已落地：
- ``health``  健康检查（``/health/live`` / ``/health/ready``）
- ``admin``   管理面 API（``/v1/admin/plugins`` 等）

后续 Phase 将在此目录追加：
- ``memory``  写入 / 检索 / 反馈 / 巩固（``/v1/memory/*``，Phase 2-6）
- ``query``   只读查询（``/v1/query/*``，Phase 7）
"""

__all__: list[str] = []
