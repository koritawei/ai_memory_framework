# Memory Service

> AI Memory 系统 —— 分层认知记忆架构的可生产服务实现。

---

## 概述

本仓库提供可独立部署的 Memory Service，覆盖写入热路径、异步冷路径、多通道检索、反馈与生命周期、离线巩固、实体/图索引及管理面等能力。业务逻辑通过插件 SPI 与统一配置中心扩展，存储与模型后端可替换。

### 测试与审计

```bash
uv run pytest tests/ -q                              # 单元与集成测试
uv run pytest tests/integration/ -m integration -v   # 故障演练（可选）
uv run python scripts/audit_no_hard_deps.py            # 业务平面零硬依赖审计
```

### 能力模块

| 模块 | 说明 | 主要入口 |
| --- | --- | --- |
| 运行时骨架 | FastAPI、健康检查、配置中心、插件注册、Prompt 管理 | `memory_app/api.py` · `memory_app/deps/` · `config_center/` · `plugins/` |
| 数据模型 | 外部/内部模型、format_transfer、SPI 抽象 | `schemas/` · `internal_models.py` · `plugins/spi/` |
| 写入热路径 | 规则/混合 SBD、MemCell、Mongo/ES/Milvus 同步写入 | `pipelines/ingest.py` · `routers/memory.py` |
| 写入冷路径 | 情景/语义抽取、MemScene 聚类、实体索引 | `pipelines/cold_path.py` · `extractors/` · `clustering.py` |
| 检索 | BM25 / 向量 / 实体 / 图多路召回，RRF 融合与 MMR 重排 | `pipelines/retrieval.py` · `retrieval/` |
| 反馈与生命周期 | 突触可塑性、遗忘曲线、FSFM 重要性 | `services.py` · `lifecycle.py` · `routers/feedback.py` |
| 离线巩固 | 三相巩固策略、容量优化、被动衰减 | `consolidation/` · `routers/memory.py` |
| 图与实体 | Entity Store、Memory Graph、图查询 API | `entity_store.py` · `graph_index.py` · `routers/query.py` |
| 管理面 | 插件与配置 CRUD、审计脚本、第三方插件示例 | `routers/admin.py` · `scripts/audit_no_hard_deps.py` |

---

## 快速开始

```bash
# 1. 安装依赖
uv sync --extra dev

# 2. 运行测试
uv run pytest tests/ -q

# 3. 插件化审计（业务平面不得直接 import plugins_default）
uv run python scripts/audit_no_hard_deps.py

# 4. 启动服务
uv run uvicorn memory_app.api:app --host 127.0.0.1 --port 8000 --reload

# 5. OpenAPI 文档
open http://127.0.0.1:8000/docs
```

### 烟测示例

```bash
curl -s http://127.0.0.1:8000/health/live
curl -s http://127.0.0.1:8000/health/ready | python3 -m json.tool

curl -s -X POST http://127.0.0.1:8000/v1/memory/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "t1",
    "user_id": "u1",
    "history_sessions": [{
      "session_id": "s1",
      "turns": [
        {"role": "user", "content": "我下周要去北京出差"},
        {"role": "assistant", "content": "好的，我帮您记下"}
      ]
    }]
  }'

curl -s -X POST http://127.0.0.1:8000/v1/memory/retrieve \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","user_id":"u1","query":"出差计划","top_k":5}'

curl -s -X POST http://127.0.0.1:8000/v1/memory/feedback \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","user_id":"u1","mem_cell_id":"<id>","feedback_type":"positive","signal_value":1.0}'

curl -s -X POST http://127.0.0.1:8000/v1/memory/consolidate \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1"}'

curl -s -X POST http://127.0.0.1:8000/v1/query/user-graph-relations \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","user_id":"u1","entity":"北京"}'

curl -s http://127.0.0.1:8000/v1/admin/plugins | python3 -m json.tool
```

鉴权：在 `config/bootstrap.yaml` 中设置 `auth_enabled=true` 并配置 `admin_api_key` 后，所有 `/v1/admin/*` 请求需携带 `X-Admin-Key` 头。

---

## HTTP 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health/live` | 进程存活 |
| GET | `/health/ready` | 就绪检查（外部依赖不可用时为 degraded） |
| POST | `/v1/memory/ingest` | 写入（热路径同步 + 冷路径异步） |
| POST | `/v1/memory/retrieve` | 多通道检索 |
| POST | `/v1/memory/feedback` | 显式/隐式反馈 |
| POST | `/v1/memory/consolidate` | 触发离线巩固 |
| POST | `/v1/query/user-graph-relations` | 实体邻域查询 |
| POST | `/v1/query/user-memories` | 用户记忆列表（分页） |
| GET/POST | `/v1/admin/plugins/*` | 插件列表、健康、重载 |
| GET/PUT/DELETE | `/v1/admin/prompts/*` | Prompt 模板 CRUD |
| GET/POST | `/v1/admin/config/*` | 运行时配置读写与回滚 |

---

## 架构

### 业务平面

```
┌──────────────── 在线写入 ────────────────┬──────────── 在线检索 ────────────┬──────── 离线认知 ────────┐
│ IngestPipeline + ColdPathPipeline        │ RetrievalPipeline (多通道)      │ Consolidation + Decay   │
└──────────────────────────────────────────┴─────────────────────────────────┴─────────────────────────┘
         ▲                                              ▲
         │ 插件 SPI · ConfigCenter · 存储适配器          │
         └──────────────────────────────────────────────┘
```

**写入路径**：`RawData → SBD → MemCell → Mongo/ES/Milvus`（同步）→ 异步 `ColdPathPipeline`（情景/语义抽取、聚类、实体索引）。

**检索路径**：`Recall → Fuse(RRF) → SignalBoost → Filter → Rerank(MMR)`，通道包括 BM25、向量、实体增强、图遍历（后两者默认可在配置中关闭）。

### 插件 SPI（节选）

| 类别 | SPI | 默认实现示例 |
| --- | --- | --- |
| 生成 | BoundaryDetector、EpisodeExtractor、SemanticExtractor、Clusterer、Consolidator、EntityExtractor | `hybrid_sbd`、`llm_episode_extractor`、`llm_10_association`、`incremental_centroid` |
| 检索 | RetrievalChannel、Fuser、Reranker、RetrievalFilter | `bm25_es_channel`、`vector_milvus_channel`、`weighted_rrf_fuser`、`mmr_reranker` |
| 存储 | KVStore、VectorStore、BM25Store、GraphStore | Mongo / ES / Milvus 仓储实现，`in_memory_lru_graph` |
| 生命周期 | ForgettingPolicy、ImportanceScorer、ConsolidationStrategy、Reinforcer | `ebbinghaus_policy`、`fsfm_scorer`、`three_phase_dreaming`、`synaptic_reinforcer` |
| Provider | EmbeddingProvider、LLMProvider、RerankProvider | 由配置指定自托管或兼容 API 的后端 |

完整扩展点见 `memory_app/plugins/spi/` 与 `memory_app/plugins_default/`。

---

## 配置

### 两类 YAML

| | `config/bootstrap.yaml` | `config/default.yaml` |
| --- | --- | --- |
| 加载方 | pydantic Settings | ConfigCenter（File 或 Mongo） |
| 内容 | 连接串、端口、鉴权、配置中心类型 | 阈值、权重、模型名、Prompt、灰度 |
| 变更 | 需重启进程 | 运行时可热更新（File 轮询或 Mongo Change Stream） |

五级覆盖：`default → global → tenant → user → request_override`，支持 `variants` 灰度规则。

生产环境可通过 `MEMORY_BOOTSTRAP_FILE` 指向独立 bootstrap 文件，例如：

```bash
MEMORY_BOOTSTRAP_FILE=/etc/memory/bootstrap.prod.yaml \
MEMORY_MONGO_URI=mongodb://prod-mongo:27017 \
MEMORY_CONFIG_CENTER_BACKEND=mongo \
uv run uvicorn memory_app.api:app --host 0.0.0.0 --port 8000
```

---

## 目录结构

```
.
├── pyproject.toml
├── README.md
├── uv.lock
├── config/
│   ├── bootstrap.yaml
│   └── default.yaml
├── docs/
│   ├── prompt-config-admin.md
│   └── runbook.md
├── examples/
│   └── memory_plugin_qdrant/
├── memory_app/
│   ├── api.py
│   ├── deps/                 # AppState、外部客户端、服务装配
│   ├── pipelines/            # ingest / cold_path / retrieval
│   ├── retrieval/            # 通道与编排
│   ├── routers/
│   ├── plugins/              # SPI 与工厂
│   ├── plugins_default/
│   ├── config_center/
│   └── ...
├── scripts/
│   └── audit_no_hard_deps.py
└── tests/
```

---

## 设计要点

**业务平面零硬依赖**：路由、服务、管线等不得 `from memory_app.plugins_default import ...`，统一通过 `PluginFactory.build()` 获取插件实例。`uv run audit-no-hard-deps` 用于 CI 静态检查。

**启动与降级**：外部依赖不可达时不阻塞进程启动，`/health/ready` 报告 degraded；检索单通道失败不拖垮整次请求。

**多租户**：对外 API 强制 `tenant_id` + `user_id`；存储与检索过滤均带租户维度。

**内外模型分离**：`schemas/` 为 HTTP 契约，`internal_models.py` 为管线内部结构，由 `format_transfer.py` 桥接。

**可选能力靠配置开启**：实体增强、图遍历等通道默认可在 `config/default.yaml` 中关闭，打开后即可参与召回，无需改业务代码。

---

## 扩展

### 内置插件

在 `memory_app/plugins_default/` 新增实现并用 `@register` 注册，随后在 `config/default.yaml` 中切换对应 `category` 的 `name`。

### 第三方插件包

参考 `examples/memory_plugin_qdrant/`：通过 `pyproject.toml` 的 `entry-points."memory_app.plugins"` 注册，安装后改配置即可切换 VectorStore 实现。

### 运维文档

- `docs/runbook.md` — 故障降级与 OnCall
- `docs/prompt-config-admin.md` — Prompt 模板运维

---

## License

MIT — 见 [LICENSE](LICENSE)。
