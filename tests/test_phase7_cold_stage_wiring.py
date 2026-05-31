"""验证 GraphComponentsBuilder._wire_phase7_cold_stage 把 EntityIndexStage 注入冷路径。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- 仅当 cold_path_service 已装配 + entity_store / memory_graph 至少一个 ready
  时,EntityIndexStage 才出现在 ColdPathPipeline 的 stages 末尾
- 已注入时再次调用为幂等(rebind 而非重复 append)
- ColdPathService 未装配 → no-op
- 全空依赖 → no-op

历史背景:此装配逻辑原位于 ``AppState._wire_phase7_cold_stage`` 私有方法,
S2 重构后移至 ``GraphComponentsBuilder._wire_phase7_cold_stage`` 静态方法。
"""

from __future__ import annotations

import pytest

from memory_app.deps import AppState
from memory_app.deps.builders.graph import GraphComponentsBuilder
from memory_app.entity_store import InMemoryEntityStore
from memory_app.graph_index import InMemoryGraph, MemoryGraph
from memory_app.pipelines import (
    ColdPathPipeline,
    EntityIndexStage,
)
from memory_app.services import ColdPathService


def _wire(state: AppState) -> None:
    """与原 ``state._wire_phase7_cold_stage()`` 等价的入口(测试便利)。"""
    GraphComponentsBuilder._wire_phase7_cold_stage(state)


def _new_state() -> AppState:
    state = AppState.__new__(AppState)
    # 仅初始化 _wire_phase7_cold_stage 需要的字段
    state.cold_path_service = None
    state.entity_store = None
    state.memory_graph = None
    state.entity_extractor = None
    return state


@pytest.mark.asyncio
class TestWirePhase7ColdStage:
    async def test_no_op_when_cold_path_service_missing(self):
        state = _new_state()
        state.entity_store = InMemoryEntityStore()
        # 不装配 cold_path_service
        _wire(state)  # 不抛即可
        assert state.cold_path_service is None

    async def test_no_op_when_no_dependencies(self):
        state = _new_state()
        pipe = ColdPathPipeline()
        state.cold_path_service = ColdPathService(pipeline=pipe, runner=None)
        _wire(state)
        # 没绑 store / graph → 不应注入 stage
        assert all(
            not isinstance(s, EntityIndexStage) for s in pipe._extra_stages  # type: ignore[attr-defined]
        )

    async def test_injects_when_entity_store_only(self):
        state = _new_state()
        pipe = ColdPathPipeline()
        state.cold_path_service = ColdPathService(pipeline=pipe, runner=None)
        state.entity_store = InMemoryEntityStore()
        _wire(state)

        injected = [s for s in pipe._extra_stages if isinstance(s, EntityIndexStage)]  # type: ignore[attr-defined]
        assert len(injected) == 1
        assert injected[0]._entity_store is state.entity_store
        assert injected[0]._memory_graph is None

    async def test_injects_when_memory_graph_only(self):
        state = _new_state()
        pipe = ColdPathPipeline()
        state.cold_path_service = ColdPathService(pipeline=pipe, runner=None)
        state.memory_graph = MemoryGraph(InMemoryGraph())
        _wire(state)

        injected = [s for s in pipe._extra_stages if isinstance(s, EntityIndexStage)]  # type: ignore[attr-defined]
        assert len(injected) == 1
        assert injected[0]._memory_graph is state.memory_graph

    async def test_idempotent_when_called_twice(self):
        state = _new_state()
        pipe = ColdPathPipeline()
        state.cold_path_service = ColdPathService(pipeline=pipe, runner=None)
        state.entity_store = InMemoryEntityStore()
        state.memory_graph = MemoryGraph(InMemoryGraph())

        _wire(state)
        _wire(state)  # 二次调用不应重复注入

        injected = [s for s in pipe._extra_stages if isinstance(s, EntityIndexStage)]  # type: ignore[attr-defined]
        assert len(injected) == 1
        # 仍绑着相同实例
        assert injected[0]._entity_store is state.entity_store
        assert injected[0]._memory_graph is state.memory_graph

    async def test_rebinds_dependencies_on_repeat_call(self):
        state = _new_state()
        pipe = ColdPathPipeline()
        state.cold_path_service = ColdPathService(pipeline=pipe, runner=None)
        state.entity_store = InMemoryEntityStore()
        _wire(state)

        # 之后 graph 才 ready,二次调用应 rebind
        state.memory_graph = MemoryGraph(InMemoryGraph())
        _wire(state)

        injected = [s for s in pipe._extra_stages if isinstance(s, EntityIndexStage)]  # type: ignore[attr-defined]
        assert len(injected) == 1
        assert injected[0]._memory_graph is state.memory_graph
