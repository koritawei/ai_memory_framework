"""VectorChannel Milvus 表达式安全测试。"""

from __future__ import annotations

import pytest

from memory_app.retrieval.channels.vector import _MILVUS_FILTER_FIELD_ALLOWLIST


def test_filter_allowlist_excludes_injection_keys():
    assert 'foo" OR tenant_id == "x' not in _MILVUS_FILTER_FIELD_ALLOWLIST
    assert "memory_type" in _MILVUS_FILTER_FIELD_ALLOWLIST


def test_milvus_eq_rejects_injection_value():
    from memory_app.retrieval.channels.vector import _milvus_eq

    with pytest.raises(ValueError):
        _milvus_eq("tenant_id", 'evil" OR 1==1')
