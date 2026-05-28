# memory-plugin-qdrant

第三方 VectorStore 插件示例(管理面)。

## 目的

证明 Memory Service 的"业务平面零硬依赖"契约真的成立 —— 你**不在主仓内**写一个
独立的 Python 包,通过 `pip install` + 改一行配置即可接管核心向量索引能力。

## 用法

```bash
# 1. 安装
uv pip install ./examples/memory_plugin_qdrant

# 2. 在 config/default.yaml 把 vector store 切到本插件
# memory.storage.vector.name: qdrant_store
# memory.storage.vector.params:
#   host: my.qdrant.example.com
#   port: 6333
#   metric: cosine

# 3. 重启 Memory Service —— 业务代码不需要任何改动
uv run uvicorn memory_app.api:app --port 8000
```

## 关键细节

- **Entry Point**:`pyproject.toml` 的 `[project.entry-points."memory_app.plugins"]`
让 `PluginRegistry.discover_entry_points` 自动找到本插件;无需主仓代码改动。
- **`@register`**:本示例同时使用了 `@register` 装饰器(便于本地 import 时也能注册)。
生产场景**只需** entry_points 即可。
- **SPI 契约**:实现 `memory_app.plugins.spi.vector_store.VectorStore` 完整 4 个方法。
契约测试 `tests/contract/vector_store_contract.py`(管理面 后续补充)对所有实现都该全绿。
- **健康检查**:`health` 返回 collection / item 数,运维通过
`GET /v1/admin/plugins/memory.storage.vector/qdrant_store/health` 实时看到。

## 替换为真 Qdrant

本示例使用进程内字典实现以避免 CI 依赖外部 Qdrant 服务。生产替换路径:

1. `pip install qdrant-client`
2. 把 `store.py` 中 `self._collections: dict[str, dict]` 换成 `QdrantClient` 实例
3. `upsert / search / delete / flush` 转发到 `QdrantClient` 同名方法
4. 业务代码 / pipeline / 配置 schema **完全不变** —— 这就是 SPI 等价性

## 与

+ 管理面:

> 第三方插件示例:发布一个 `memory_plugin_qdrant` 独立小包到 `examples/`,演示
> "写一个包,pip install 后无侵入接管 VectorStore"
