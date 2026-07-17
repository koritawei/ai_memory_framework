# Memory Service

> AI Memory 系统 —— 分层认知记忆架构。本仓库实现《Memory 系统方案设计 —— 分层认知记忆架构》中的可生产服务。
>
> 设计文档：`../docs/Memory 系统方案设计 —— 分层认知记忆架构.md`

---

## 当前状态：Phase 0 – 8 全部完成 ✅

> 一站式可启动的完整 Memory Service:写入热路径 + 异步冷路径 + 五通道检索 + 反馈生命周期 + 离线巩固 + 实体/图增强 + 管理面 + 第三方插件示例。

### 测试规模

```
747 passed in tests/                 # 默认 pytest;含 12 个第三方插件契约测试
  6 passed in tests/integration/     # 显式 -m integration 触发(故障演练)
插件化审计通过 (16 个业务平面入口)     # uv run python scripts/audit_no_hard_deps.py
```

### 各 Phase 落地能力

| Phase | 核心产出 | 入口 / 文件 |
| --- | --- | --- |
| **0** 脚手架 | FastAPI + lifespan + 健康检查 + 配置中心 + 插件 SPI 骨架 + Prompt 管理 | `api.py` · `deps.py` · `config_center/` · `plugins/` · `prompt_manager/` |
| **1** 数据模型 | 外部/内部模型 + format_transfer + 30+ SPI ABC | `schemas/` · `internal_models.py` · `format_transfer.py` · `plugins/spi/` |
| **2** 写入热路径 | 规则 SBD + MemCell + Mongo/ES/Milvus 三库写入 | `pipelines/ingest.py` · `routers/memory.py:ingest` |
| **3** 写入冷路径 | LLM SBD + 情景/语义抽取 + MemScene 聚类 | `pipelines/cold_path.py` · `extractors/` · `clustering.py` |
| **4** 检索管线 | BM25 + Vector + RRF + MMR + Threshold | `pipelines/retrieval.py` · `retrieval/` · `routers/memory.py:retrieve` |
| **5** 反馈与生命周期 | 突触可塑性 Reinforcer + Ebbinghaus + FSFM 重要性 | `services.py:FeedbackService` · `lifecycle.py` · `scoring.py` · `routers/feedback.py` |
| **6** 离线巩固 | Composite Consolidator + 三相 dreaming + 容量优化 + 被动衰减 | `consolidator.py` · `consolidation/` · `routers/memory.py:consolidate` |
| **7** 图与实体 | Entity Store + Memory Graph + Entity Boost + 图遍历通道 + 只读图查询 | `entity_store.py` · `graph_index.py` · `retrieval/channels/{entity,graph}.py` · `routers/query.py` |
| **8** 管理面 + 收口 | 业务平面零硬依赖审计 + Mongo Change Stream + Admin Config CRUD + 故障演练 + 第三方插件示例 | `scripts/audit_no_hard_deps.py` · `config_center/mongo_center.py` · `routers/admin.py` · `examples/memory_plugin_qdrant/` |

---

## 快速开始

```bash
cd memory/

# 0. 可选：复制环境变量样例
cp .env.example .env

# 1. 安装依赖（首次约 30s）
uv sync --extra dev

# 2. 跑测试（应 747 passed）
uv run pytest tests/ -q

# 3. 跑插件化审计（业务平面零硬依赖,设计文档 §2.7.5）
uv run python scripts/audit_no_hard_deps.py

# 4. 跑故障演练（4 类降级 + 单通道超时）
uv run pytest tests/integration/ -m integration -v

# 5. 启动服务
uv run uvicorn memory_app.api:app --host 127.0.0.1 --port 8000 --reload

# 6. 访问 OpenAPI 自动文档
open http://127.0.0.1:8000/docs
```

### 烟测命令

```bash
# 进程存活 / 就绪
curl -s http://127.0.0.1:8000/health/live
curl -s http://127.0.0.1:8000/health/ready | python3 -m json.tool

# 写入(热路径同步入库 + 冷路径异步抽取)
curl -s -X POST http://127.0.0.1:8000/v1/memory/ingest \
  -H 'Content-Type: application/json' -d '{
    "tenant_id":"t1","user_id":"u1",
    "history_sessions":[{"session_id":"s1","turns":[
      {"role":"user","content":"我下周要去北京出差"},
      {"role":"assistant","content":"好的,我帮您记下"}
    ]}]}'

# 检索(BM25 + Vector + RRF + MMR)
curl -s -X POST http://127.0.0.1:8000/v1/memory/retrieve \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","user_id":"u1","query":"出差计划","top_k":5}'

# 反馈(触发 strength 突触可塑性更新)
curl -s -X POST http://127.0.0.1:8000/v1/memory/feedback \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","user_id":"u1","mem_cell_id":"<id>","feedback_type":"positive","signal_value":1.0}'

# 离线巩固(三相 dreaming + 容量优化)
curl -s -X POST http://127.0.0.1:8000/v1/memory/consolidate \
  -H 'Content-Type: application/json' -d '{"tenant_id":"t1"}'

# 图查询(Phase 7)
curl -s -X POST http://127.0.0.1:8000/v1/query/user-graph-relations \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","user_id":"u1","entity":"北京"}'

# 管理面(Phase 8 / 已注册插件 + 配置 CRUD)
curl -s http://127.0.0.1:8000/v1/admin/plugins | python3 -m json.tool
curl -s 'http://127.0.0.1:8000/v1/admin/config?category=memory.retrieval.fuser'
curl -s -X POST http://127.0.0.1:8000/v1/admin/config \
  -H 'Content-Type: application/json' \
  -d '{"category":"memory.retrieval.fuser","name":"weighted_rrf","params":{"k":80}}'
```

---

## HTTP 端点总览

| 方法 | 路径 | Phase | 说明 |
| --- | --- | --- | --- |
| GET | `/health/live` | 0 | 进程存活 |
| GET | `/health/ready` | 0 | 就绪含外部依赖检查(degraded≠fail) |
| POST | `/v1/memory/ingest` | 2-3 | 写入热路径(同步)+ 异步冷路径排队 |
| POST | `/v1/memory/retrieve` | 4 | 五通道检索(BM25/Vector/Entity/Graph + RRF + MMR) |
| POST | `/v1/memory/feedback` | 5 | 显/隐式反馈 → 突触可塑性更新 strength |
| POST | `/v1/memory/consolidate` | 6 | 触发离线三相 dreaming + 容量优化 |
| POST | `/v1/query/user-graph-relations` | 7 | 实体邻域 mem_cell_id 列表 |
| POST | `/v1/query/user-memories` | 7 | 用户记忆列表(分页) |
| GET | `/v1/admin/plugins` | 0 | 列出已注册插件 + 活动实例 |
| GET | `/v1/admin/plugins/health` | 0 | 聚合插件实例健康 |
| GET | `/v1/admin/plugins/{category}/{name}/health` | 8 | 单插件实例健康 |
| POST | `/v1/admin/plugins/{category}/{name}/reload` | 8 | 手工触发释放 + 重建 |
| GET/PUT/DELETE | `/v1/admin/prompts/*` | 0.7 | Prompt CRUD + history + 试渲染 |
| GET | `/v1/admin/config` | 8 | 读当前生效配置(含 source 命中层) |
| POST | `/v1/admin/config` | 8 | 写配置(JSON Schema 校验 + 版本化) |
| GET | `/v1/admin/config/history` | 8 | 配置历史 |
| POST | `/v1/admin/config/rollback` | 8 | 回滚(前进式:把旧版本作为新写入) |
| GET | `/v1/admin/dlq` | 8 | 列出 DLQ 记录(需 X-Admin-Key) |
| GET | `/v1/admin/dlq/stats` | 8 | DLQ 积压统计(按 target) |
| POST | `/v1/admin/dlq/reconcile` | 8 | 手动触发 ES/Milvus 索引重试 |
| GET | `/metrics` | 8 | Prometheus 指标(`metrics_enabled=true`) |

鉴权:业务面 ``auth_enabled=true`` 时要求 Bearer;管理面在配置了 ``admin_api_key`` 时
**始终**要求 ``X-Admin-Key``(与 ``auth_enabled`` 无关)。

---

## 架构总览

### 四个平面

```
┌─────────────────────── 业务平面（三平面） ──────────────────────────┐
│  在线写入 (§5)            │   在线检索 (§6)         │   离线认知 (§7)  │
│  Phase 2 + 3 ✅           │   Phase 4 + 7 ✅        │   Phase 6 ✅     │
│  IngestPipeline           │   RetrievalPipeline     │   ConsolidationStrategy │
│  ColdPathPipeline         │   (5 channels)          │   + DecayManager        │
└───────────────────────────┴─────────────────────────┴─────────────────┘
                  ▲                                  ▲
                  │依赖                               │下发
                  │                                  │
┌─────── 横切平面：插件 SPI（Phase 0-8） ──────┐  ┌─── 横切平面：配置中心 ───┐
│ Plugin / Registry / Factory + audit script  │  │ ConfigCenter (A+B 嵌套)  │
│ + 30+ SPI ABC 扩展点 ✅                      │  │ 五级覆盖 + 灰度路由       │
│ + 第三方包 entry-point 自动 discover         │  │ + Mongo Change Stream     │
│ + 业务平面零硬依赖 CI 守门                    │  │ + 版本化 + 回滚           │
└────────────────────────────────────────────┘  └─────────────────────────┘
```

### 写入双路径

```
RawData ─→ SBD ─→ MemCell ─→ PersistMemCellStage(Mongo+ES+Milvus) ─→ HTTP 200
                              │
                              └─→ ColdPathPipeline (异步, BackgroundTaskRunner)
                                     ├── EpisodeExtractStage  (LLM 情景抽取)
                                     ├── SemanticExtractStage (LLM 语义联想)
                                     ├── ClusterStage         (MemScene 聚类)
                                     └── EntityIndexStage     (Phase 7: 写 EntityStore + MemoryGraph)
```

### 检索五通道 + 五阶段

```
RetrieveMemRequest
        │
        ▼
┌──── RecallStage (并发, 单路超时隔离) ────┐
│  bm25_es ─┐                             │
│  vector_milvus ─┤                       │
│  entity_boost ──┼──→ channel_outputs    │
│  graph_traversal ┘                      │
└─────────────────────────────────────────┘
        │
   FuseStage (weighted_rrf, 默认权重 bm25:0.4 vector:0.6)
        ↓
   SignalBoostStage (RRFScore × TimeDecay × (1 + Importance))
        ↓
   FilterStage (threshold)
        ↓
   RerankStage (mmr, 可选 cross_encoder)
        ↓
   list[RankedMemory]
```

### 30+ SPI 扩展点 + 默认实现矩阵

| 类别 | SPI | 默认实现 |
| --- | --- | --- |
| **生成 (9)** | BoundaryDetector / EpisodeExtractor / SemanticExtractor / EventLogExtractor / ProfileExtractor / Clusterer / Consolidator / EntityExtractor / ValueDiscriminator | `rule_sbd` / `llm_sbd` / `hybrid_sbd` · `llm_episode_extractor` · `llm_10_association` · `incremental_centroid` · `composite_consolidator` · `regex_entity_extractor` |
| **检索 (6)** | RetrievalChannel × 4 / Fuser / Reranker / RetrievalFilter / QueryRewriter / IntentClassifier | `bm25_es_channel` · `vector_milvus_channel` · `entity_boost_channel` · `graph_traversal_channel` · `weighted_rrf_fuser` / `noop_fuser` · `mmr_reranker` · `threshold_filter` |
| **存储 (7)** | KVStore / VectorStore / BM25Store / GraphStore / CacheStore / IdempotencyStore / DLQStore | `in_memory_lru_graph` · 主仓 mongo/es/milvus client 直接绑入 |
| **生命周期 (5)** | ForgettingPolicy / ImportanceScorer / ConsolidationStrategy / CapacityOptimizer / Reinforcer | `ebbinghaus_policy` · `fsfm_scorer` · `three_phase_dreaming` · `greedy_capacity_optimizer` · `synaptic_reinforcer` |
| **Provider (3)** | EmbeddingProvider / LLMProvider / RerankProvider | 用户配置 (deepinfra / anthropic / 自托管) |

---

## 配置体系

### 两类 YAML，职责严格分离

| 维度 | `config/bootstrap.yaml` | `config/default.yaml` |
| --- | --- | --- |
| **加载者** | pydantic Settings | FileConfigCenter |
| **承载内容** | DB URI / 端口 / 鉴权 / 配置中心后端选择 | 业务策略：阈值 / 权重 / 模型名 / cron / 灰度 / Prompt |
| **加载时机** | 进程启动一次 | 运行时 + watch 热更新 |
| **修改影响** | 必须重启进程 | 立即生效（≤5s 缓存 TTL） |
| **路径来源** | 默认 `config/bootstrap.yaml`(可经 `MEMORY_BOOTSTRAP_FILE` 重定向) | 由 `bootstrap.yaml` 中 `config_center_file_path` 字段指定 |

### 五级覆盖 + 灰度路由（设计文档 §2.8）

```
default → global → tenant → user → request_override
                  │
                  └─ variants[] 灰度匹配（tenant_id_in / user_id_hash_mod_100_lt / ...）
```

写入时 JSON Schema 校验、版本号自增、historic ring buffer (File) 或 Mongo history collection。

### MongoConfigCenter 生产化（Phase 8 Step 8.2）

- `coll.watch(full_document="updateLookup")` Change Stream 后台 task
- 抖断后 1s → 30s 指数退避重连
- 多副本 / 多进程**互不感知**地共享同一 ConfigCenter:写入立即被所有副本 watch 到
- 副本集 / 分片集群之外(standalone)优雅降级:仅靠 5s TTL 缓存兜底

### Settings 优先级

```
init kwargs > 环境变量 (MEMORY_*) > config/bootstrap.yaml > .env > secrets
```

**关键约束**：Settings 字段在代码中**禁止**写硬编码默认值（仅 `Optional[str] = None` 例外），缺失即 `ValidationError`。由 `tests/test_settings.py::test_no_hardcoded_field_defaults` 在 CI 中守护。

### 生产部署示例

```bash
MEMORY_BOOTSTRAP_FILE=/etc/memory/bootstrap.prod.yaml \
MEMORY_MONGO_URI=mongodb://prod-mongo-cluster:27017 \
MEMORY_CONFIG_CENTER_BACKEND=mongo \
MEMORY_AUTH_ENABLED=true \
MEMORY_ADMIN_API_KEY=$(vault read -field=key secret/memory/admin) \
uv run uvicorn memory_app.api:app --host 0.0.0.0 --port 8000
```

---

## 运维与可观测性

### 独立 Redis Worker

API 与 Worker 分离时,API 只入队冷路径任务,由独立进程消费 Redis 队列。

**bootstrap.yaml 关键项:**

```yaml
task_runner_backend: redis
task_runner_consumer_enabled: false   # API 不消费,交给 worker
task_queue_key: memory:tasks
redis_url: redis://redis:6379/0
dlq_backend: mongo                    # 或 redis
```

**启动 Worker:**

```bash
# 安装后
memory-worker

# 开发态
uv run python -m memory_app.worker
```

```
┌─────────────┐     LPUSH      ┌─────────────┐
│  memory-api │ ─────────────► │    Redis    │
│ (consumer   │                │   queue     │
│  disabled)  │                └──────┬──────┘
└─────────────┘                       │ BRPOP
                               ┌──────▼──────┐
                               │memory-worker│  (可水平扩展 N 副本)
                               └─────────────┘
```

DLQ 重试与 Admin 端点详见 [docs/worker-and-dlq.md](docs/worker-and-dlq.md)。

### Prometheus 指标

1. 在 `config/bootstrap.yaml` 开启:

```yaml
metrics_enabled: true
dlq_reconcile_interval_s: 300          # 可选:API 后台自动 reconcile(秒)
dlq_reconcile_batch_size: 100
dlq_reconcile_max_retries: 5
```

2. 验证端点:

```bash
curl -s http://127.0.0.1:8000/metrics | head
```

3. 配置 Prometheus 抓取 —— 完整示例见 [docs/prometheus/scrape-config.example.yaml](docs/prometheus/scrape-config.example.yaml):

```yaml
scrape_configs:
  - job_name: memory-service
    metrics_path: /metrics
    static_configs:
      - targets: ["memory-api:8000"]
        labels:
          service: memory
          env: production
```

**主要指标:**

| 指标 | 说明 |
| --- | --- |
| `memory_http_requests_total` | HTTP 请求计数(按 path/status) |
| `memory_http_request_duration_seconds` | 请求延迟直方图 |
| `memory_dlq_records` | DLQ 当前积压条数 |
| `memory_dlq_reconcile_success_total` / `memory_dlq_reconcile_failure_total` | Reconcile 成功/失败 |
| `memory_background_tasks_submitted_total` / `memory_background_tasks_failed_total` | 后台任务提交/DLQ |
| `memory_rate_limit_rejected_total` | 限流 429(启用 `rate_limit_enabled` 时) |

### Grafana 仪表盘

1. 确保 Prometheus 已抓取 `/metrics`(见上节)
2. Grafana → **Dashboards** → **Import**
3. 上传 [`docs/grafana/memory-service-dashboard.json`](docs/grafana/memory-service-dashboard.json)
4. 选择 Prometheus 数据源

面板覆盖 HTTP QPS/延迟、DLQ 积压、Reconcile、5xx 比例、429 限流等。导入步骤与告警建议见 [docs/grafana/README.md](docs/grafana/README.md)。

**手动 DLQ Reconcile 示例:**

```bash
curl -X POST http://localhost:8000/v1/admin/dlq/reconcile \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target": "es", "limit": 100, "dry_run": false}'
```

---

## 目录结构

```
memory/
├── pyproject.toml                    # entry_points + audit-no-hard-deps script
├── README.md
├── scripts/
│   └── audit_no_hard_deps.py         # Phase 8.1: 业务平面零硬依赖审计
│
├── config/
│   ├── bootstrap.yaml                # Settings 来源(启动期不可变)
│   └── default.yaml                  # ConfigCenter 来源(运行时可调)
│
├── docs/
│   ├── prompt-config-admin.md        # Prompt 运维说明(Phase 0.7)
│   ├── runbook.md                    # OnCall SOP(Phase 8.4)
│   ├── worker-and-dlq.md             # Worker 部署 + DLQ Reconcile 管理端点
│   ├── prometheus/
│   │   └── scrape-config.example.yaml # Prometheus 抓取与告警示例
│   └── grafana/
│       ├── README.md                 # 仪表盘导入说明
│       └── memory-service-dashboard.json
│
├── examples/
│   └── memory_plugin_qdrant/         # 第三方插件示例(Phase 8.4)
│       ├── pyproject.toml            # entry_points 注册
│       ├── memory_plugin_qdrant/
│       │   ├── __init__.py
│       │   └── store.py              # QdrantVectorStore: VectorStore SPI 实现
│       └── README.md
│
├── memory_app/
│   ├── api.py                        # FastAPI 入口 + lifespan
│   ├── settings.py                   # 启动期不可变配置(YAML+env)
│   ├── deps.py                       # AppState 单例 + 11 步 init
│   ├── prompt_runtime.py             # Prompt manager 运行时绑定
│   │
│   ├── format_transfer.py            # 外部 → 内部 RawData 转换
│   ├── internal_models.py            # MemCell / EpisodicMemory / SemanticMemory / RankedMemory
│   ├── sbd.py                        # SBD 通用工具
│   ├── clustering.py                 # MemScene 增量聚类
│   ├── consolidator.py               # 离线巩固通用算法
│   ├── lifecycle.py                  # 反馈触发的 strength/access_count 更新
│   ├── scoring.py                    # FSFM 四维评分
│   ├── entity_store.py               # Phase 7: entity → mem_cell_ids 倒排索引
│   ├── graph_index.py                # Phase 7: MemoryGraph + InMemoryGraph
│   ├── services.py                   # IngestService/ColdPathService/FeedbackService/ConsolidationService
│   ├── background.py                 # BackgroundTaskRunner + 重试 + DLQ
│   ├── worker/                       # 独立 Redis 冷路径 Worker (`memory-worker`)
│   ├── middleware/                   # metrics / rate_limit / tenant_binding
│   ├── reconciliation/               # SyncReconciler(DLQ → ES/Milvus 重试)
│   ├── task_queue/                   # Redis 分布式任务队列
│   ├── security/                     # auth + JWT/API Key 租户绑定   │
│   ├── schemas/                      # 外部 API 契约
│   │   ├── ingest.py · retrieve.py · feedback.py
│   │
│   ├── routers/                      # FastAPI 路由
│   │   ├── health.py                 # /health/live · /health/ready
│   │   ├── memory.py                 # /v1/memory/{ingest,retrieve,consolidate}
│   │   ├── feedback.py               # /v1/memory/feedback
│   │   ├── query.py                  # /v1/query/user-graph-relations · /user-memories
│   │   └── admin.py                  # /v1/admin/{plugins,prompts,config}/*
│   │
│   ├── pipelines/                    # 三条管线(§2.7.6)
│   │   ├── base.py                   # BasePipeline + PipelineStage
│   │   ├── ingest.py                 # SegmentStage → PersistMemCellStage → SyncIndexStage
│   │   ├── cold_path.py              # EpisodeExtract → SemanticExtract → Cluster → EntityIndex
│   │   └── retrieval.py              # Recall → Fuse → SignalBoost → Filter → Rerank
│   │
│   ├── retrieval/                    # 检索通道与编排
│   │   ├── channels/{base,bm25,vector,entity,graph}.py
│   │   ├── fusion.py · reranker.py · orchestrator.py
│   │
│   ├── extractors/                   # LLM 抽取器(冷路径)
│   │   ├── episode_extractor.py · semantic_extractor.py
│   ├── consolidation/                # Phase 6 离线
│   │   ├── decay.py · sleep.py
│   ├── repositories/                 # 各存储后端 CRUD
│   │   ├── mongo_repo.py · es_repo.py · milvus_repo.py · dlq.py
│   ├── prompt_manager/               # Prompt 模板管理(Phase 0.7)
│   │   ├── manager.py · config_backed.py · builtins.py · models.py
│   │
│   ├── plugins/                      # SPI 抽象层
│   │   ├── base.py                   # Plugin / PluginMeta / PluginError
│   │   ├── registry.py               # PluginRegistry + @register
│   │   ├── factory.py                # PluginFactory + release_category + health_of
│   │   └── spi/                      # 30+ SPI ABC
│   │
│   ├── plugins_default/              # 23 个默认实现
│   │   ├── 生成: rule_sbd / llm_sbd / hybrid_sbd
│   │   │      llm_episode_extractor / llm_10_association
│   │   │      incremental_centroid / composite_consolidator / regex_entity_extractor
│   │   ├── 检索: bm25_es_channel / vector_milvus_channel
│   │   │      entity_boost_channel / graph_traversal_channel
│   │   │      weighted_rrf_fuser / noop_fuser / mmr_reranker / threshold_filter
│   │   ├── 存储: in_memory_lru_graph
│   │   ├── 生命周期: ebbinghaus_policy / fsfm_scorer / synaptic_reinforcer
│   │   │           three_phase_dreaming / greedy_capacity_optimizer
│   │   └── stub: noop_sbd / noop_fuser
│   │
│   └── config_center/                # 统一配置中心(§2.8)
│       ├── base.py                   # ConfigCenter ABC + ConfigChangeEvent
│       ├── _common.py                # BaseConfigCenter 通用流程
│       ├── _db.py                    # DBConfigCenter DB 共享层
│       ├── resolver.py               # 五级覆盖 + 灰度
│       ├── schema.py / prompt_schema.py
│       ├── _prompts.py               # PromptConfigMixin
│       ├── prompt_paths.py
│       ├── file_center.py            # YAML 后端
│       └── mongo_center.py           # Mongo 后端 + Change Stream(Phase 8.2)
│
└── tests/
    ├── conftest.py · fixtures/
    ├── test_*.py                     # 747 个默认 pytest 用例
    ├── contract/
    │   └── test_vector_store_contract.py    # SPI 等价性守门人(12 用例)
    └── integration/
        └── test_degradation.py       # 4 类故障演练(@pytest.mark.integration, 6 用例)
```

---

## 关键设计决策

### 1) 业务平面零硬依赖（Phase 8 Step 8.1 强制）

业务平面**永远**通过 `PluginFactory.build(category, tenant_id, user_id)` 取插件实例，**禁止**直接 `from memory_app.plugins_default.* import *`。

`scripts/audit_no_hard_deps.py` 通过 AST 静态扫描 16 个业务入口(routers / services / retrieval / pipelines / repositories / extractors / consolidation 等),CI 接入:

```bash
uv run audit-no-hard-deps     # 0 = 通过, 1 = 有违规
```

测试 `tests/test_audit_no_hard_deps.py` 还会构造伪仓验证审计能识别人为引入的硬依赖。

### 2) 启动期不可变 vs 运行时可调（设计文档 §2.8.6.2）

| 维度 | 启动期不可变 (Settings) | 运行时可调 (ConfigCenter) |
| --- | --- | --- |
| 例子 | `mongo_uri` / `auth_enabled` / `config_center_backend` | SBD 阈值 / RRF k 值 / 模型名 / cron / Prompt 模板 |
| 修改影响 | 重启进程 | 热更新（File: mtime 轮询;Mongo: Change Stream + TTL 兜底） |

### 3) 延迟可达性（设计文档 §5.4）

- 外部依赖(Mongo / ES / Milvus / Redis / LLM / Embedding)不可达**不**阻塞启动 —— 仅在 `/health/ready` 上报 `degraded`
- 11 步 init 每步独立 try/except,失败仅 warn,不影响其他子项
- 检索路径单通道异常 → ctx 记录 + 继续;**全部失败**才抛 `PluginError(all_channels_failed, retryable=True)`

### 4) 多租户隔离根（设计文档 §5.2.2）

- 所有外部 API 请求**强制**携带 `tenant_id` + `user_id`，缺一即 `ValidationError`
- 所有记忆体 / 向量 / BM25 / Graph 节点的 filters 必含 `tenant_id` 实现物理隔离

### 5) 内/外解耦

- `schemas/` = 对外契约（OpenAPI 暴露给客户端）
- `internal_models.py` = 内部数据（业务平面消费）
- `format_transfer.py` = 二者之间的桥
- API 可独立演进而不冲击内部实现

### 6) 能力解锁纯靠配置

Phase 7 / 8 的高级能力(Entity Boost / Graph Traversal / Fisher-Rao)**默认关闭**:

```yaml
memory:
  retrieval:
    channels:
      entity:
        enabled: false
      graph:
        enabled: false
```

灰度时只翻 `enabled: true` + 调权重 `weights.entity: 0.15`,业务代码**零改动**,EntityIndexStage 在写入路径上始终运行,索引随写积累 —— 翻开关即生效。

---

## 开发与扩展

### 添加内置插件

```python
# memory_app/plugins_default/sbd_my_impl.py
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.boundary_detector import BoundaryDetector

@register
class MyHybridSBD(BoundaryDetector):
    meta = PluginMeta(
        name="my_hybrid_sbd",
        category="memory.generation.boundary_detector",
        version="1.0.0",
        config_schema={...},
    )
    async def start(self, config): ...
    async def stop(self): ...
    async def detect(self, history, new, ctx): ...
```

修改 `config/default.yaml` 把 `memory.generation.boundary_detector.name` 切到 `my_hybrid_sbd`,**重启服务即生效**,业务代码零改动。

### 添加第三方插件包(无侵入)

完整可运行示例:`examples/memory_plugin_qdrant/`。

```toml
# 第三方项目的 pyproject.toml
[project.entry-points."memory_app.plugins"]
qdrant_store = "memory_plugin_qdrant.store:QdrantVectorStore"
```

```bash
uv pip install ./examples/memory_plugin_qdrant
# 改 config/default.yaml: memory.storage.vector.name = "qdrant_store"
# 重启服务即生效,业务代码 / Pipeline / 配置 schema 完全不变
```

契约测试 `tests/contract/test_vector_store_contract.py` 对所有 `VectorStore` 实现都该全绿 —— 这是 SPI 等价性的守门人。

### 添加新 ConfigCenter 后端

| 场景 | 继承自 | 工作量 |
| --- | --- | --- |
| PostgreSQL（CRUD 模型） | `DBConfigCenter` | 9 个 `_db_*` 方法 |
| etcd（KV 树 + Watch RPC） | `BaseConfigCenter` | 4 个 hook |
| Apollo（HTTP long-poll） | `BaseConfigCenter` | 4 个 hook |

复用 `tests/test_base_config_center.py` 或 `test_db_config_center.py` 作契约测试,任何后端必须通过同一套用例。

### 故障演练 + OnCall SOP

`docs/runbook.md` 列出 4 类故障(LLM / Embedding / ES / Milvus 不可用)的:

- 触发条件 + Prometheus 告警 label
- 自动降级行为
- OnCall 命令(`POST /v1/admin/config` 切备用 / `reload` 强刷)
- 回滚步骤

集成测试 `tests/integration/test_degradation.py` 把这些场景固化到 CI:

```bash
uv run pytest tests/integration/ -m integration -v   # 6 个降级用例
```

---

## 参考

- 主设计文档:`../docs/Memory 系统方案设计 —— 分层认知记忆架构.md`
- OnCall Runbook: `docs/runbook.md`
- Worker 与 DLQ: `docs/worker-and-dlq.md`
- Prometheus 抓取示例: `docs/prometheus/scrape-config.example.yaml`
- Grafana 仪表盘: `docs/grafana/README.md`
- Prompt 运维: `docs/prompt-config-admin.md`
- 第三方插件示例: `examples/memory_plugin_qdrant/README.md`
- 关键章节:
  - §2.5 FastAPI 形态
  - §2.7 可插拔插件架构(SPI)
  - §2.8 统一配置中心
  - §3 输入层数据格式 / §4 内部数据标准
  - §5.1 写入六阶段 / §5.4 服务降级表
  - §6 检索五阶段 / §6.5 Entity Boost 与 Graph
  - §7 离线认知平面 / §8 Memory Graph
  - 附录 A 完整落地方案 (Phase 0–8)
