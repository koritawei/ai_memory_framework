"""FastAPI 路由集合。

脚手架 已落地：
- ``health``  健康检查（``/health/live`` / ``/health/ready``）
- ``admin``   管理面 API（``/v1/admin/plugins`` 等）

后续路由将在此目录追加：
- ``memory``  写入 / 检索 / 反馈 / 巩固（``/v1/memory/*``，写入热路径-6）
- ``query``   只读查询（``/v1/query/*``，图与实体）
"""

__all__: list[str] = []
