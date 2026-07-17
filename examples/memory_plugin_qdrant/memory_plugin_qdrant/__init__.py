"""memory_plugin_qdrant —— 示例第三方 VectorStore 插件(Phase 8 Step 8.4)。

═══════════════════════════════════════════════════════════════════════════════
为什么要有这个示例
═══════════════════════════════════════════════════════════════════════════════
设计文档明确:**插件化是 Memory Service 的最高约束**(§2.7)。要证明这套架构
真的实现了"业务平面零硬依赖",就必须有一个完全独立、不在主仓内的插件包能
通过 ``pip install`` 接管核心能力。

本示例演示:
1. ``[project.entry-points."memory_app.plugins"]`` 在 ``pyproject.toml`` 注册
2. 实现 :class:`memory_app.plugins.spi.vector_store.VectorStore` SPI
3. 注册 ``@register`` (或交由 entry_points 自动注册)
4. 用户**只需**改 ``default.yaml`` 把 ``memory.storage.vector.name`` 切到
   ``qdrant_store`` 就生效,业务代码零改动

本实现使用进程内 dict 模拟 Qdrant,**不**调真实 Qdrant 服务,因此可作为契约
测试 fixture 使用。生产场景请把 ``store.py`` 中的字典换成 ``qdrant-client``。
"""

from .store import QdrantVectorStore

__all__ = ["QdrantVectorStore"]
