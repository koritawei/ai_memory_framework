"""Quality-loop Iteration 2 的 regression 锁定测试。

每个 case 对应一条本轮 本轮 修复。旧代码下 case 失败,新代码下通过。
"""

from __future__ import annotations

import asyncio

import pytest

from memory_app.internal_models import MemCell, MemoryState


# ════════════════════════════════════════════════════════════════════════════
# Regression #1 (Iter2 A1):PluginFactory request_override 必须 bypass cache
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_factory_request_override_does_not_pollute_cache():
    """旧实现:第一次带 request_override(="ovr-A") 的 build 把实例缓存进去 ——
    第二次不带 override 的 build 拿到的还是 ovr-A 的实例,override 语义被破坏。
    新实现:request_override 非 None 时 bypass cache,每次新建。
    """
    from memory_app.plugins.base import Plugin, PluginMeta
    from memory_app.plugins.factory import PluginFactory
    from memory_app.plugins.registry import PluginRegistry

    # 自建 registry 避免污染全局(register 会自动入全局,这里我们 bypass 它)
    class _SpyPlugin(Plugin):
        meta = PluginMeta(
            name="spy_qloop",
            category="memory.demo.qloop",
            version="1.0.0",
            description="iter2 regression",
            config_schema={
                "type": "object",
                "additionalProperties": True,
                "properties": {"flag": {"type": "string", "default": "base"}},
            },
        )

        def __init__(self) -> None:
            self.started_with_flag: str | None = None

        async def start(self, config):
            self.started_with_flag = config.get("flag")

        async def stop(self):
            return None

    registry = PluginRegistry()
    registry.register(_SpyPlugin)

    # mock ConfigCenter(连 watch 都 mock 掉,避免触发 attach 后的订阅链)
    class _MockCC:
        async def resolve(self, category, **kw):
            from memory_app.config_center.base import ResolvedPluginConfig
            ovr = kw.get("request_override")
            # request_override 直接体现到 params.flag,模拟用户期望
            flag = (ovr or {}).get(category, {}).get("params", {}).get("flag", "base")
            return ResolvedPluginConfig(
                name="spy_qloop",
                params={"flag": flag},
                version=1,
                source="default",
            )

        async def watch(self, callback):
            pass

        async def close(self):
            pass

    factory = PluginFactory(registry=registry)
    await factory.attach_config_center(_MockCC())

    # 1st build:带 override-A,期望 flag="ovr-A"
    inst_a = await factory.build(
        "memory.demo.qloop",
        request_override={
            "memory.demo.qloop": {"params": {"flag": "ovr-A"}}
        },
    )
    assert inst_a.started_with_flag == "ovr-A"

    # 2nd build:无 override,期望 flag="base"(全新实例)—— 旧代码会返回 inst_a
    inst_b = await factory.build("memory.demo.qloop")
    assert inst_b.started_with_flag == "base"
    assert inst_b is not inst_a  # 必须是不同实例(override 路径不进 cache)

    # 3rd build:无 override 再来一次 → 此时应命中正常 cache
    inst_c = await factory.build("memory.demo.qloop")
    assert inst_c is inst_b, "无 override 路径应正常缓存"


# ════════════════════════════════════════════════════════════════════════════
# Regression #2 (Iter2 A2):builder 通过公开 API 接入,不 reach 私有字段
# ════════════════════════════════════════════════════════════════════════════
def test_retrieval_orchestrator_exposes_add_recall_channel():
    """RetrievalOrchestrator 必须暴露 add_recall_channel 公开方法。"""
    from memory_app.retrieval.orchestrator import RetrievalOrchestrator
    assert hasattr(RetrievalOrchestrator, "add_recall_channel"), (
        "RetrievalOrchestrator 必须暴露 add_recall_channel 公开方法,"
        "否则 GraphComponentsBuilder 又得 reach 私有 _recall"
    )


def test_cold_path_service_exposes_attach_stage():
    """ColdPathService 必须暴露 attach_stage / find_extra_stage 公开方法。"""
    from memory_app.services import ColdPathService
    assert hasattr(ColdPathService, "attach_stage"), (
        "ColdPathService 必须暴露 attach_stage 公开方法,"
        "否则 GraphComponentsBuilder 又得 reach 私有 _pipeline._extra_stages"
    )
    assert hasattr(ColdPathService, "find_extra_stage")


def test_cold_path_pipeline_exposes_add_extra_stage():
    """ColdPathPipeline 必须暴露 add_extra_stage / find_extra_stage 公开方法。"""
    from memory_app.pipelines.cold_path import ColdPathPipeline
    assert hasattr(ColdPathPipeline, "add_extra_stage")
    assert hasattr(ColdPathPipeline, "find_extra_stage")


# ════════════════════════════════════════════════════════════════════════════
# Regression #3 (Iter2 A3):validator 不再每次重新编译
# ════════════════════════════════════════════════════════════════════════════
def test_validator_is_cached_across_calls_for_same_schema():
    """相同 schema 多次调用 validate_params,内部 validator 实例必须是同一个。"""
    from memory_app.config_center.schema import _get_validator, validate_params

    schema = {
        "type": "object",
        "properties": {"k": {"type": "integer", "default": 60}},
    }
    v1 = _get_validator(schema)
    v2 = _get_validator(schema)
    assert v1 is v2, "相同 schema 应该复用同一 Draft202012Validator 实例"

    # 不同 schema 必须分开
    other = {"type": "object", "properties": {"k": {"type": "string"}}}
    v3 = _get_validator(other)
    assert v3 is not v1

    # 端到端 validate_params 行为不变
    out = validate_params({"k": 5}, schema)
    assert out == {"k": 5}


# ════════════════════════════════════════════════════════════════════════════
# Regression #4 (Iter2 B1):FSFMScorer.detail 子分只算一次
# ════════════════════════════════════════════════════════════════════════════
def test_fsfm_detail_does_not_double_compute_subscores():
    """detail 应该 cqa/bve/trs/src 各算 1 次(共 4 次),
    旧实现 detail 调 score 内又算一遍 → 8 次。
    """
    from memory_app.scoring import FSFMScorer

    counter = {"cqa": 0, "bve": 0, "trs": 0, "src": 0}
    # 用 instance-level 属性 monkey-patch 而非类属性,避开 staticmethod / instance method
    # 装饰器混合的麻烦(cqa/bve/src 是 @staticmethod;trs 是带 self 的实例方法)。
    # 实例上挂同名属性会覆盖类查找,且能正确绑定 self。
    scorer = FSFMScorer()
    cell = MemCell(
        tenant_id="t1", user_id="u1", session_id="s1",
        text="hello", strength=2.0, access_count=3,
    )

    real_cqa = FSFMScorer.cqa_score
    real_bve = FSFMScorer.bve_score
    real_src = FSFMScorer.src_score
    bound_trs = scorer.trs_score  # 已绑定到 scorer 实例

    def _make_static_spy(name, fn):
        def _inner(c):
            counter[name] += 1
            return fn(c)
        return _inner

    def _trs_spy(c, n):
        counter["trs"] += 1
        return bound_trs(c, n)

    # 在 instance 上覆盖 —— 不动类
    scorer.cqa_score = _make_static_spy("cqa", real_cqa)  # type: ignore[method-assign]
    scorer.bve_score = _make_static_spy("bve", real_bve)  # type: ignore[method-assign]
    scorer.trs_score = _trs_spy  # type: ignore[method-assign]
    scorer.src_score = _make_static_spy("src", real_src)  # type: ignore[method-assign]

    scorer.detail(cell)
    assert counter == {"cqa": 1, "bve": 1, "trs": 1, "src": 1}, (
        f"detail 调用次数 {counter} —— 旧实现是 2/2/2/2(double-compute)"
    )


# ════════════════════════════════════════════════════════════════════════════
# Regression #5 (Iter2 B2):FileConfigCenter 提供 async I/O 包装
# ════════════════════════════════════════════════════════════════════════════
def test_file_config_center_has_async_io_wrappers():
    """_persist_entry 应通过 _read_raw_async / _write_raw_async 走 to_thread,
    避免在 asyncio.Lock 持有期间阻塞事件循环。
    """
    from memory_app.config_center.file_center import FileConfigCenter

    assert hasattr(FileConfigCenter, "_read_raw_async"), (
        "FileConfigCenter 应暴露 _read_raw_async,_persist_entry 用它替代同步 _read_raw"
    )
    assert hasattr(FileConfigCenter, "_write_raw_async")


# ════════════════════════════════════════════════════════════════════════════
# Regression #6 (Iter2 B3):background.py DLQRecord 已移到模块顶部
# ════════════════════════════════════════════════════════════════════════════
def test_background_dlqrecord_imported_at_module_top():
    """模块顶部应能直接拿到 DLQRecord 引用(以前是函数内 lazy import)。"""
    import memory_app.background as bg
    assert hasattr(bg, "DLQRecord")
    from memory_app.repositories.dlq import DLQRecord as _R
    assert bg.DLQRecord is _R


# ════════════════════════════════════════════════════════════════════════════
# Regression #7 (Iter2 Pass1):validator cache 必须真正持有强引用
# ════════════════════════════════════════════════════════════════════════════
def test_validator_cache_uses_strong_reference_not_weakref():
    """旧实现 WeakValueDictionary 让 validator 被立即 GC,等价无 cache。
    必须用强引用 dict,validator 持有到进程结束。
    """
    from memory_app.config_center import schema as schema_mod

    # 内部缓存应为普通 dict(不是 WeakValueDictionary)
    assert isinstance(schema_mod._validator_cache, dict)
    assert "weak" not in type(schema_mod._validator_cache).__name__.lower()

    # 端到端:连续两次调 _get_validator 应返回同一 validator,即使中间触发 GC
    import gc
    s = {"type": "object", "properties": {"x": {"type": "integer"}}}
    v1 = schema_mod._get_validator(s)
    del v1
    gc.collect()
    v2 = schema_mod._get_validator(s)
    # 旧 WeakValueDictionary 实现:v2 是新建的;新强引用实现:v2 复用
    assert id(v2) in (id(x) for x in schema_mod._validator_cache.values()), (
        "validator 应被强引用 cache 持有"
    )


# ════════════════════════════════════════════════════════════════════════════
# Regression #8 (Iter2 Pass1):CLI Emitter fmt=json + str 输出合法 JSON
# ════════════════════════════════════════════════════════════════════════════
def test_cli_emitter_json_mode_quotes_string_payload():
    """`--output json` 时 str payload 必须输出带引号的 JSON 字符串字面量,
    否则下游 `jq` 等管道工具 parse 失败。
    """
    from io import StringIO
    from memory_app.cli.output import Emitter

    s = StringIO()
    Emitter(s, fmt="json").emit("hello")
    out = s.getvalue().strip()
    assert out == '"hello"', f"json 模式应输出带引号的字符串字面量,实际: {out!r}"

    # raw 模式仍然透传(向后兼容)
    s2 = StringIO()
    Emitter(s2, fmt="raw").emit("hello")
    assert s2.getvalue().strip() == "hello"


# ════════════════════════════════════════════════════════════════════════════
# Regression #9 (Iter2 Pass1):admin render_prompt 拒绝非 dict variables
# ════════════════════════════════════════════════════════════════════════════
def test_admin_render_prompt_rejects_non_dict_variables():
    """旧实现 ``payload.get("variables") or {}`` 把 [] / "" / 0 都吞成 {},
    422 校验形同虚设。新实现应严格区分 None(默认空) vs 非 dict(422)。
    """
    from fastapi.testclient import TestClient
    from memory_app import api
    from memory_app.config_center.base import ConfigCenter
    from memory_app.deps.state import app_state

    class _StubCC(ConfigCenter):
        async def resolve(self, *a, **kw): raise NotImplementedError
        async def write(self, *a, **kw): return 1
        async def history(self, *a, **kw): return []
        async def watch(self, callback): pass
        async def close(self): pass

    saved_cc = app_state.config_center
    app_state.config_center = _StubCC()
    try:
        with TestClient(api.app) as client:
            r = client.post(
                "/v1/admin/prompts/foo/render",
                json={"variables": []},  # 非 dict
            )
            # 必须 400,不是 200(旧实现把 [] 吞成 {} 后走到 _format_template)
            assert r.status_code == 400, (
                f"非 dict variables 应 400;实际 {r.status_code}: {r.text}"
            )
            assert "must be a mapping" in r.text
    finally:
        app_state.config_center = saved_cc


# ════════════════════════════════════════════════════════════════════════════
# Regression #10 (Iter2 Pass1):FileConfigCenter._reload_mu 升级为 asyncio.Lock
# ════════════════════════════════════════════════════════════════════════════
def test_file_config_center_persist_uses_asyncio_lock():
    """_persist_entry 跨 to_thread await 持锁,必须用 asyncio.Lock 而非
    threading.Lock —— 否则 _poll_loop 同步获取 threading.Lock 时会阻塞事件循环。
    """
    from memory_app.config_center.file_center import FileConfigCenter
    # 新增字段
    assert hasattr(FileConfigCenter, "__init__")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        f.write(b"defaults: {}\n")
        path = f.name
    cc = FileConfigCenter(yaml_path=path)
    # 验证类型
    assert isinstance(cc._reload_mu_async, asyncio.Lock)


# ════════════════════════════════════════════════════════════════════════════
# Regression #11 (Iter2 Pass2):FileConfigCenter 统一一把 asyncio.Lock
# ════════════════════════════════════════════════════════════════════════════
def test_file_config_center_has_single_async_lock_only():
    """Pass1 引入了 asyncio.Lock 但旧 threading.Lock(_reload_mu)还留着,
    两把锁不互斥 —— _poll_loop 走 threading 路径,_persist_entry 走 async,
    并发时还会丢更新。Pass2 必须只保留 asyncio.Lock。
    """
    import tempfile
    from memory_app.config_center.file_center import FileConfigCenter

    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        f.write(b"defaults: {}\n")
        path = f.name
    cc = FileConfigCenter(yaml_path=path)
    # 旧 threading.Lock 必须移除(由 _reload_mu_async 替代覆盖所有路径)
    assert not hasattr(cc, "_reload_mu") or cc._reload_mu is None, (
        "_reload_mu(threading.Lock)应被移除,统一用 _reload_mu_async"
    )
    # async reload 方法存在
    assert hasattr(FileConfigCenter, "_reload_from_disk_async")
    # 同步 initial 也应存在(__init__ 走它)
    assert hasattr(FileConfigCenter, "_reload_from_disk_initial")
