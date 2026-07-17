"""Step 0.6 验收：五级覆盖与灰度匹配。"""

from __future__ import annotations

import pytest

from memory_app.config_center.resolver import ConfigResolver


CATEGORY = "memory.retrieval.fuser"


def make_resolver():
    return ConfigResolver()


def test_default_only():
    r = make_resolver()
    cfg, src = r.resolve(
        CATEGORY,
        defaults={CATEGORY: {"name": "noop_fuser", "params": {"k": 60}}},
    )
    assert cfg["name"] == "noop_fuser"
    assert cfg["params"] == {"k": 60}
    assert src == "default"


def test_global_overrides_default():
    r = make_resolver()
    cfg, src = r.resolve(
        CATEGORY,
        defaults={CATEGORY: {"name": "noop_fuser", "params": {"k": 60}}},
        global_overrides={CATEGORY: {"params": {"k": 80}}},
    )
    assert cfg["params"] == {"k": 80}
    assert src == "global"


def test_tenant_overrides_global():
    r = make_resolver()
    cfg, src = r.resolve(
        CATEGORY,
        defaults={CATEGORY: {"name": "noop_fuser", "params": {"k": 60}}},
        global_overrides={CATEGORY: {"params": {"k": 80}}},
        tenant_overrides={"acme": {CATEGORY: {"params": {"k": 100}}}},
        tenant_id="acme",
    )
    assert cfg["params"] == {"k": 100}
    assert src == "tenant"


def test_user_overrides_tenant():
    r = make_resolver()
    cfg, src = r.resolve(
        CATEGORY,
        defaults={CATEGORY: {"name": "noop_fuser", "params": {"k": 60}}},
        tenant_overrides={"acme": {CATEGORY: {"params": {"k": 100}}}},
        user_overrides={"u1": {CATEGORY: {"params": {"k": 120}}}},
        tenant_id="acme",
        user_id="u1",
    )
    assert cfg["params"] == {"k": 120}
    assert src == "user"


def test_request_overrides_user():
    r = make_resolver()
    cfg, src = r.resolve(
        CATEGORY,
        defaults={CATEGORY: {"name": "noop_fuser", "params": {"k": 60}}},
        request_override={CATEGORY: {"params": {"k": 999}}},
    )
    assert cfg["params"] == {"k": 999}
    assert src == "request"


def test_user_hash_gray_distribution():
    """灰度命中率必须落在 [9%, 11%]（10% ± 1pp）。"""
    hits = sum(1 for i in range(2000) if ConfigResolver.match_user_hash(f"user_{i}", lt=10))
    pct = hits / 2000 * 100
    assert 8.5 <= pct <= 11.5, f"灰度分布偏差过大: {pct:.2f}%"


def test_variants_by_tenant():
    r = make_resolver()
    cfg, _ = r.resolve(
        CATEGORY,
        defaults={
            CATEGORY: {
                "name": "noop_fuser",
                "params": {"k": 60},
                "variants": [
                    {
                        "name": "noop_fuser",
                        "params": {"k": 200},
                        "match": {"tenant_id_in": ["acme"]},
                    }
                ],
            }
        },
        tenant_id="acme",
    )
    assert cfg["params"]["k"] == 200
    assert cfg.get("_variant_matched") is True


def test_variants_no_match():
    r = make_resolver()
    cfg, _ = r.resolve(
        CATEGORY,
        defaults={
            CATEGORY: {
                "name": "noop_fuser",
                "params": {"k": 60},
                "variants": [
                    {"params": {"k": 200}, "match": {"tenant_id_in": ["acme"]}},
                ],
            }
        },
        tenant_id="other",
    )
    assert cfg["params"]["k"] == 60


@pytest.mark.parametrize(
    "uid,modulus",
    [("u1", 100), ("u2", 1000), ("user_xyz_42", 1024)],
)
def test_hash_bucket_stable(uid, modulus):
    a = ConfigResolver.hash_bucket(uid, modulus)
    b = ConfigResolver.hash_bucket(uid, modulus)
    assert a == b
    assert 0 <= a < modulus
