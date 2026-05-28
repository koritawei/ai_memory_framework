# Memory 系统测试套件

本目录实现《Memory 系统完整测试方案》中的**可执行测试套件**，与 `memory/tests/` 内置单元/组件测试互补。

## 目录结构

```
test_suite/
├── README.md
├── conftest.py          # 共用 fixture（项目根、隔离配置、TestClient）
├── fixtures/            # 测试样本数据
├── e2e/                 # 端到端 / 流程级用例（对应方案 ）
├── gates/               # CI 门禁（审计等）
├── runner.py            # 统一执行器 + 报告生成
└── reports/             # 运行产物（JUnit XML、报告 Markdown）
```

## 快速运行

```bash
cd memory/

# 安装依赖
uv sync --extra dev

# 执行完整测试方案并生成报告
uv run python test_suite/runner.py

# 仅跑 E2E + 门禁
uv run pytest test_suite/e2e test_suite/gates -v

# 仅跑性能烟测
uv run pytest test_suite/nft -m nft -v

# 报告输出默认：docs/Memory 系统测试报告.md
```

## 与 memory/tests 的关系

| 层级 | 位置 | 说明 |
| --- | --- | --- |
| 单元/组件/API | `memory/tests/` | 747+ 用例，日常开发回归 |
| 集成降级 | `memory/tests/integration/` | `@pytest.mark.integration` |
| 契约 | `memory/tests/contract/` | SPI 等价性 |
| **方案 E2E / 门禁编排** | `memory/test_suite/` | 按测试方案 – 编排执行并出报告 |
