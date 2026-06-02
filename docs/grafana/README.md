# Memory Service — Grafana 仪表盘

## 前置条件

1. `config/bootstrap.yaml` 中设置 `metrics_enabled: true`
2. Prometheus 抓取 Memory Service 的 `/metrics` 端点 —— 完整示例见 [`../prometheus/scrape-config.example.yaml`](../prometheus/scrape-config.example.yaml)

## 导入仪表盘

1. 打开 Grafana → **Dashboards** → **Import**
2. 上传 [`memory-service-dashboard.json`](./memory-service-dashboard.json)
3. 选择 Prometheus 数据源

## 面板说明

| 面板 | 指标 | 用途 |
|------|------|------|
| HTTP 请求速率 | `memory_http_requests_total` | 流量与热点路径 |
| HTTP 延迟 | `memory_http_request_duration_seconds` | p50/p95 延迟 |
| DLQ 积压 | `memory_dlq_records` | 三库同步失败积压 |
| DLQ Reconcile | `memory_dlq_reconcile_*_total` | 重试成功/失败 |
| 5xx 比例 | 5xx / total | 可用性 |
| 后台任务 | `memory_background_tasks_*` | 冷路径/DLQ |
| 429 速率 | status=429 | 限流触发 |

## 告警建议

- `memory_dlq_records > 100` 持续 10m → 索引同步异常
- `rate(memory_dlq_reconcile_failure_total[5m]) > 0.1` → Reconcile 失败
- `histogram_quantile(0.95, ...) > 2` → 检索/写入延迟过高
