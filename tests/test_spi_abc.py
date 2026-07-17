"""Step 1.4 验收：所有 SPI ABC 反射检查（设计文档 §2.7.2 / Phase 1 落地确认）。"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import memory_app.plugins.spi as spi_pkg
from memory_app.plugins.base import Plugin


def _enumerate_spi_classes():
    """反射枚举 spi/ 下所有 Plugin 子类（且尚未具体实现）。"""
    found: list[type[Plugin]] = []
    for m in pkgutil.iter_modules(spi_pkg.__path__):
        mod = importlib.import_module(f"{spi_pkg.__name__}.{m.name}")
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if not issubclass(obj, Plugin) or obj is Plugin:
                continue
            # 排除：在 SPI 文件中复用 import 的具体类（如 NoopSBD 不应出现 —— 但 SPI 文件不该 import 它）
            # 仍是抽象的 Plugin 子类才算 SPI
            if not getattr(obj, "__abstractmethods__", None):
                continue
            # 同名重复（例如多文件 import）只记一次
            if obj not in found:
                found.append(obj)
    return found


def test_at_least_30_spi_abc():
    """设计文档 §2.7.2 总表共 30 个扩展点，全部应已落地。"""
    found = _enumerate_spi_classes()
    assert len(found) >= 30, f"应至少 30 个 SPI ABC，实际 {len(found)}"


def test_all_spi_inherit_plugin():
    found = _enumerate_spi_classes()
    for cls in found:
        assert issubclass(cls, Plugin), f"{cls.__name__} 必须继承 Plugin"


def test_all_spi_have_abstractmethods():
    found = _enumerate_spi_classes()
    for cls in found:
        abstracts = getattr(cls, "__abstractmethods__", None)
        assert abstracts, f"{cls.__name__} 必须有至少一个 @abstractmethod"


def test_spi_cannot_be_instantiated():
    """ABC 不可直接实例化（必须先实现所有 abstractmethod）。"""
    found = _enumerate_spi_classes()
    for cls in found:
        with pytest.raises(TypeError):
            cls()


def test_spi_dunder_init_does_not_throw_at_class_level():
    """import 期不应有任何错误（如循环依赖、字段类型解析失败）。"""
    # 导入触发；上面 fixture 已隐式做过，但显式再来一遍
    for m in pkgutil.iter_modules(spi_pkg.__path__):
        importlib.import_module(f"{spi_pkg.__name__}.{m.name}")


# ── 关键 SPI 的具体存在性断言（防止类名重命名漏迁移）──
EXPECTED_SPI_NAMES = {
    # 生成（9）
    "BoundaryDetector", "EpisodeExtractor", "SemanticExtractor",
    "EventLogExtractor", "ProfileExtractor", "Clusterer",
    "Consolidator", "EntityExtractor", "ValueDiscriminator",
    # 检索（6）
    "RetrievalChannel", "Fuser", "Reranker",
    "RetrievalFilter", "QueryRewriter", "IntentClassifier",
    # 存储（7）
    "KVStore", "VectorStore", "BM25Store",
    "GraphStore", "CacheStore", "IdempotencyStore", "DLQStore",
    # 生命周期（5）
    "ForgettingPolicy", "ImportanceScorer", "ConsolidationStrategy",
    "CapacityOptimizer", "Reinforcer",
    # Provider（3）
    "EmbeddingProvider", "LLMProvider", "RerankProvider",
}


def test_all_expected_spis_present():
    found = {cls.__name__ for cls in _enumerate_spi_classes()}
    missing = EXPECTED_SPI_NAMES - found
    assert not missing, f"缺失 SPI ABC：{missing}"


def test_noop_plugins_now_implement_real_spi():
    """Phase 1 起 noop_sbd / noop_fuser 应继承自正式 SPI。"""
    from memory_app.plugins.spi.boundary_detector import BoundaryDetector
    from memory_app.plugins.spi.fuser import Fuser
    from memory_app.plugins_default.noop_fuser import NoopFuser
    from memory_app.plugins_default.noop_sbd import NoopSBD

    assert issubclass(NoopSBD, BoundaryDetector)
    assert issubclass(NoopFuser, Fuser)
