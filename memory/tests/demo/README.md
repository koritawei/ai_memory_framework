# Demo Tests —— 端到端走读式测试

> 这一目录里的测试不是用来"找 bug"的(那是 `tests/test_*.py` 的工作),
> 而是把"一条业务请求在系统内的完整路径"写成可执行的、带断言的文档。

## 适用场景

- **新人 onboarding**:从上到下读一个 demo,就能理解某条业务管线"请求进来发生了什么"
- **活文档**:任何阶段的契约破裂(SBD 不切了 / RRF 算错了 / 异步任务被吞)都会让对应 demo 立刻失败
- **零外部依赖**:Mongo / ES / Milvus / LLM / Embedding 全部 mock,本地一行 `pytest` 即跑

## 阅读顺序(沿"写 → 读 → 反馈 → 巩固"业务时间线)

| 顺序 | 文件 | 覆盖能力 | 关键概念 |
| --- | --- | --- | --- |
| 1 | `test_demo_ingest_hot_path.py` | 写入热路径 | RawData → SBD → MemCell → Mongo + ES + Milvus + DLQ |
| 2 | `test_demo_cold_path.py` | 冷路径 + 图索引 | Episode 抽取 → Semantic 抽取 → 聚类 → EntityIndex |
| 3 | `test_demo_retrieval_pipeline.py` | 检索 | RecallStage → FuseStage(RRF)→ SignalBoost → Filter → Rerank(MMR) |
| 4 | `test_demo_feedback_lifecycle.py` | 反馈与生命周期 | POSITIVE / NEGATIVE / EXPLICIT_CONFIRM 反馈 → atomic strength 更新 |
| 5 | `test_demo_sleep_consolidation.py` | 离线巩固 | MemScene → LLM 候选 → Consolidator 决策(ADD/UPDATE/SUPERSEDE/NOOP) |

## 运行方式

```bash
# 单独跑 demo
.venv/bin/pytest tests/demo/ -v

# 跑某一个 demo
.venv/bin/pytest tests/demo/test_demo_retrieval_pipeline.py -v

# 跟主测试套件一起跑(demo 默认被 pytest tests/ 自动收集)
.venv/bin/pytest tests/ -q
```

## 设计原则

1. **线性、可读**:demo 是"脚本"不是"参数化矩阵"。每条 demo 顺序就是阅读顺序。
2. **重注释**:每段代码上方说明"现在系统里在做什么、为什么这么做"。
3. **强断言**:不止断言"返回值正确",还断言每个 stage 对外部 fake 的副作用 ——
一个 stage 偷偷退化也能被立刻发现。
4. **真实管线 + 虚假叶子**:Pipeline / Service / Consolidator 等业务代码全部走真实实现;
只把"叶子"(Mongo / ES / Milvus / LLM / Embedding)替换成 fake。
5. **fixture 隔离**:demo 的 fake 都在 `tests/demo/conftest.py`,不污染主测试套件的命名空间。

## 与其他测试目录的关系

| 目录 | 关注点 | 典型问题 |
| --- | --- | --- |
| `tests/test_*.py`(主套件) | 单点正确性 | "这个函数的边界条件对不对" |
| `tests/contract/`        | SPI 等价性 | "我换一个 VectorStore 实现还能跑通吗" |
| `tests/integration/`     | 故障演练   | "ES 真的挂了系统会怎样" |
| **`tests/demo/`**(本目录) | **端到端走读** | **"一条 ingest 请求在系统内会经过哪些阶段"** |

## 增加新 demo 的建议

- 关注**业务流程**而不是单元算法 —— 一个 demo 跨多个 stage / service
- 输入应"看上去像真实业务"(中文人话 / 真实场景)而不是 `"x" / "y"`
- 每个断言后写一行中文注释,说明这条断言为什么重要
- 保持每个文件 ≤ 300 行 / ≤ 6 个测试函数 —— 超出就拆分

## fixture 速查

`conftest.py` 提供:

| Fixture | 类型 | 等价于 |
| --- | --- | --- |
| `fake_mongo` | `FakeMongoRepo` | `MongoMemCellRepo`(含 `atomic_apply_strength_delta`) |
| `fake_es` | `FakeESClient` | `motor` 风格 ES 客户端 |
| `fake_milvus` | `FakeMilvusInsert` | `MilvusMemCellRepo.insert_callable` |
| `fake_entity_store` | `FakeEntityStore` | `EntityStore` |
| `fake_memory_graph` | `FakeMemoryGraph` | `MemoryGraph` 业务门面 |
| `make_cell(text, ...)` | helper | 构造 `MemCell` 的便利函数 |

所有 fake 都在 `tests/demo/conftest.py`,源代码即文档。
