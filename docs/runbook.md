# OnCall Runbook(Phase 8 Step 8.4)

围绕 §5.4 降级表的 4 类故障 + 第三方插件接入 SOP。

## 故障总览

| 故障               | 用户可见症状           | 自动降级                      | OnCall 动作                            |
| ------------------ | ---------------------- | ----------------------------- | -------------------------------------- |
| LLM Provider 不可用 | ingest 写入仍 200,但冷路径任务积压 | 冷路径整体 skip,标记 `pending_llm_extraction` | 看 BackgroundTaskRunner backlog;评估降级时长 |
| Embedding 不可用   | 新写入数据无 vector,只能命中 BM25 | 跳过 Milvus 写入,标记 `pending_vectorize` | 拉起 Embedding,触发 reconciler 补写 |
| Elasticsearch 不可用 | 检索缺关键词路    | RRF 融合从两路退化为 vector-only | docker compose 重启 ES;跑 BM25 回填 |
| Milvus 不可用      | 检索缺向量语义路   | RRF 融合从两路退化为 BM25-only | docker compose 重启 Milvus;跑向量回填 |

---

## 1. LLM Provider 不可用

**触发条件**:Anthropic / DeepInfra 域名连续 5 分钟超时 / 5xx;Prometheus
告警 `degraded_mode_active{service="llm"} == 1`。

**自动降级行为**:
- `AppState._init_cold_path_service()` 检测到 LLM 不可达 → 整个冷路径**不启动**
- 已运行的实例:`BackgroundTaskRunner` 的 LLM 调用任务进入 DLQ + 重试

**OnCall SOP**:
```bash
# 1. 确认上游可达性
curl -s -o /dev/null -w "%{http_code}\n" https://api.anthropic.com/v1/messages

# 2. 看积压
curl -s -H "X-Admin-Key: $ADMIN_KEY" \
  http://memory-svc/v1/admin/plugins/health | jq '.'

# 3. 灰度切到备用 LLM
curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  http://memory-svc/v1/admin/config -d '{
    "category":"memory.provider.llm","name":"deepinfra_qwen",
    "params":{"model":"Qwen/Qwen3-72B-Instruct"}}'
sleep 60   # 等 ConfigCenter watcher 推送 + PluginFactory reload

# 4. 验证恢复
curl -s -X POST http://memory-svc/v1/memory/ingest -d @sample.json
```

**回滚**:`POST /v1/admin/config/rollback {"category":"memory.provider.llm","target_version":<前一版>}`

---

## 2. Embedding Provider 不可用

**触发条件**:Embedding API 5xx;`degraded_mode_active{service="embedding"} == 1`。

**自动降级行为**:
- 写入热路径:Vector 通道 skip,MemCell 仅写 Mongo + ES,Milvus 跳过 + 打 `pending_vectorize` flag
- 检索:Vector 通道 retrieve 抛 PluginError → orchestrator 隔离 + RRF 退化为 BM25-only

**OnCall SOP**:同 LLM 节奏;灰度切备用 Embedding 后,`POST /v1/admin/plugins/memory.provider.embedding/{name}/reload` 强制刷新。

**长尾**:Embedding 恢复后,跑离线 reconciler 补写 `pending_vectorize=true` 的记录:
```bash
curl -s -X POST http://memory-svc/v1/memory/consolidate -d '{"tenant_id":"...","scope":"reconcile_vectors"}'
```

---

## 3. Elasticsearch 不可用

**触发条件**:`http://es:9200` 连接拒绝 / 集群 yellow >5min。

**自动降级行为**:`RecallStage` 内 BM25 通道抛 `PluginError(es_unavailable)` → `ctx.channel_warnings["bm25"]` 记录 + 继续。`RetrievalOrchestrator` 仍对外返回 vector-only 结果。

**OnCall SOP**:
```bash
# 1. 看实例健康
curl -s -H "X-Admin-Key: $ADMIN_KEY" \
  http://memory-svc/v1/admin/plugins/memory.retrieval.channels.bm25/bm25_es/health

# 2. 重启 ES
docker compose -f tests/integration/compose.yaml restart es
# 等待 _cluster/health 变 green

# 3. 强制刷新通道
curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" \
  http://memory-svc/v1/admin/plugins/memory.retrieval.channels.bm25/bm25_es/reload
```

**回填**:可选 — ES 期间没写入(因为 ingest 热路径会因 `es=fail` 也降级跳过 ES 写入)。恢复后跑:
```bash
curl -s -X POST http://memory-svc/v1/memory/consolidate -d '{"scope":"reconcile_es"}'
```

---

## 4. Milvus 不可用

**触发条件**:`milvus:19530` 连接拒绝 >5min。

**自动降级行为**:Vector 通道 retrieve 抛 PluginError → 与 ES 故障对偶,RRF 退化为 BM25-only。

**OnCall SOP**:同 ES,把 BM25 → Vector 替换即可。

---

## 5. 第三方插件接入(VectorStore 替换为 Qdrant)

**前置**:`examples/memory_plugin_qdrant/` 含完整可独立分发的小包。

```bash
# 1. 安装(本地或私有 PyPI)
uv pip install ./examples/memory_plugin_qdrant

# 2. 改 config/default.yaml 把 vector store 切到 qdrant_store
# memory:
#   storage:
#     vector:
#       name: qdrant_store
#       params: { host: my.qdrant.example.com, port: 6333, metric: cosine }

# 3. 重启 Memory Service
uv run uvicorn memory_app.api:app --port 8000

# 4. 看活动实例确认接管
curl -s -H "X-Admin-Key: $ADMIN_KEY" http://memory-svc/v1/admin/plugins | \
  jq '.active[] | select(.category=="memory.storage.vector")'
# 期望:name=qdrant_store
```

**契约保证**:`tests/contract/test_vector_store_contract.py` 对 `QdrantVectorStore`
全绿。任何替换实现都必须通过这套用例,否则会破坏业务平面隐式期待。

---

## 通用工具速查

```bash
# 审计:业务平面零硬依赖
uv run python scripts/audit_no_hard_deps.py

# 列出所有插件 + 健康
curl -s -H "X-Admin-Key: $ADMIN_KEY" http://memory-svc/v1/admin/plugins | jq '.'

# 单实例健康
curl -s -H "X-Admin-Key: $ADMIN_KEY" \
  http://memory-svc/v1/admin/plugins/memory.retrieval.channels.bm25/bm25_es/health

# 配置 CRUD
curl -s -H "X-Admin-Key: $ADMIN_KEY" \
  "http://memory-svc/v1/admin/config?category=memory.retrieval.fuser"

# 配置历史
curl -s -H "X-Admin-Key: $ADMIN_KEY" \
  "http://memory-svc/v1/admin/config/history?category=memory.retrieval.fuser&limit=20"

# rollback
curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  http://memory-svc/v1/admin/config/rollback \
  -d '{"category":"memory.retrieval.fuser","target_version":3}'
```
