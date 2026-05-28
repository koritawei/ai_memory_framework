"""Demo tests —— 走读式的端到端 walkthrough。

这一目录里的测试不是用来"找 bug"的(那是 tests/test_*.py 的工作),而是:

1. **新人 onboarding**:照着 demo 文件从上往下读,可以理解每条业务管线"一条请求
   进来,中间发生了什么"。
2. **活文档**:任何阶段的契约破裂(SBD 不切了 / RRF 算错了 / 异步任务被吞了)
   都会让对应 demo 立刻失败。
3. **零外部依赖**:Mongo / ES / Milvus / LLM / Embedding 全部 mock,跑测试不需要
   起任何后端 —— 与 tests/integration/ 的 docker 演练正交。

阅读顺序建议:

- ``test_demo_ingest_hot_path.py``       写入热路径
- ``test_demo_cold_path.py``             冷路径（异步 LLM 抽取）
- ``test_demo_retrieval_pipeline.py``    检索五阶段管线
- ``test_demo_feedback_lifecycle.py``    反馈强化（突触可塑性）
- ``test_demo_sleep_consolidation.py``   离线巩固（三种 ConsolidationDecision）

每个 demo 文件都是"线性脚本":没有 parametrize 花活,从输入构造到管线触发再到
断言,阅读顺序与代码执行顺序一致。
"""
