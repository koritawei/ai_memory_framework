# Worker 与 DLQ 运维

## 独立 Redis Worker

API 与 Worker 分离部署时，API 只入队、Worker 专消费冷路径任务。

### 配置

```yaml
# bootstrap.yaml / default.yaml
task_runner_backend: redis
task_runner_consumer_enabled: false   # API 进程不消费
task_queue_key: memory:tasks
dlq_backend: mongo                    # 或 redis
```

### 启动

```bash
# 安装后
memory-worker

# 或开发态
python -m memory_app.worker
```

Worker 要求 `task_runner_backend=redis`，会 BRPOP 消费 `task_queue_key` 队列中的 `cold_path` 等任务。

### 典型拓扑

```
┌─────────────┐     LPUSH      ┌─────────────┐
│  memory-api │ ─────────────► │    Redis    │
│ (consumer   │                │   queue     │
│  disabled)  │                └──────┬──────┘
└─────────────┘                       │ BRPOP
                               ┌──────▼──────┐
                               │memory-worker│
                               │  (N 副本)   │
                               └─────────────┘
```

---

## DLQ Reconciler 管理端点

需 `X-Admin-Key`（与 `admin_api_key` 一致）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/admin/dlq` | 列出 DLQ 记录，`?target=es&limit=50` |
| GET | `/v1/admin/dlq/stats` | 按 target 统计积压 |
| POST | `/v1/admin/dlq/reconcile` | 手动触发重试 |

### 手动 Reconcile 示例

```bash
curl -X POST http://localhost:8000/v1/admin/dlq/reconcile \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target": "es", "limit": 100, "dry_run": false}'
```

响应字段：`scanned`、`succeeded`、`failed`、`skipped`、`exhausted`、`details[]`。

### 自动 Reconcile

`dlq_reconcile_interval_s > 0` 时，API 进程后台定时扫描（默认 batch 见 `dlq_reconcile_batch_size`）。生产环境可只开 Worker + 手动/定时 reconcile，interval 设为 0 关闭 API 内循环。

### Reconcile 逻辑

1. 扫描 DLQ（仅处理 `target in (es, milvus)`）
2. 从 Mongo SOT 读取 MemCell
3. 重试写入 ES / Milvus
4. 成功 → `remove`；失败 → `bump_retry`；超过 `dlq_reconcile_max_retries` → `exhausted`

---

## Grafana 监控

见 [grafana/README.md](./grafana/README.md) 导入 `memory-service-dashboard.json`。Prometheus 抓取配置见 [prometheus/scrape-config.example.yaml](./prometheus/scrape-config.example.yaml)。

关键指标：`memory_dlq_records`、`memory_dlq_reconcile_success_total`、`memory_dlq_reconcile_failure_total`。
