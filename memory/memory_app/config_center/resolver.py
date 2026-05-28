"""五级覆盖与灰度匹配。

═══════════════════════════════════════════════════════════════════════════════
本模块只承载"纯函数 / 无状态"的解析逻辑
═══════════════════════════════════════════════════════════════════════════════
- 五级覆盖合并：default → global → tenant → user → request
- 灰度变体匹配：5 维度（tenant_id_in / user_id_hash_mod_100_lt /
  traffic_pct / time_range / tag_in）

后端类（File / DB / etcd）拿到 4 个 hook 提供的原始 overrides 后，
都通过 :class:`ConfigResolver` 完成最终解析。
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from memory_app._compat import utcnow


# ════════════════════════════════════════════════════════════════════════════
# 内部工具：合并、哈希、时间窗、流量百分比
# ════════════════════════════════════════════════════════════════════════════
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深合并：override 中的同 key 覆盖 base，dict 递归合并，list/标量直接替换。

    list 不做合并是刻意的 —— 让"覆盖一个 channels 列表"语义保持直观。
    """
    out = deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _user_hash_bucket(user_id: str, modulus: int = 100) -> int:
    """对 user_id 做稳定哈希取模，返回 ``[0, modulus)``。

    使用 MD5 而非 SHA256：MD5 在分布均匀性上够用且更快；这里不涉及安全。
    取前 4 字节 = 32 bit，足以覆盖 ``modulus`` 直至 4 亿桶。
    """
    digest = hashlib.md5(user_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % modulus


def _match_time_range(rule: list[str] | None, now: datetime | None = None) -> bool:
    """判定当前时刻是否落在 ``[start, end]`` 区间内。

    ``rule`` 应为 ``["2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"]`` 风格。
    解析失败时返回 True（不阻断），让运维通过 logs 排查格式问题。
    """
    if not rule or len(rule) != 2:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        start = datetime.fromisoformat(rule[0])
        end = datetime.fromisoformat(rule[1])
    except (ValueError, TypeError):
        return True
    # 容忍 naive datetime（无时区），假设 UTC
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start <= now <= end


def _match_traffic_pct(pct: float | int | None, user_id: str | None) -> bool:
    """流量百分比命中。

    - 有 user_id：用 ``hash(user_id) % 10000 < pct * 100`` 做稳定分桶 —— 同一
      用户多次请求要么都命中、要么都不命中（不会因每次请求随机而抖动）
    - 无 user_id：退化为按时间分钟分桶（同分钟内请求得到相同结果）
    """
    if pct is None:
        return True
    try:
        pct_val = float(pct)
    except (TypeError, ValueError):
        return True
    if pct_val >= 100:
        return True
    if pct_val <= 0:
        return False
    if user_id:
        return _user_hash_bucket(user_id, 10000) < pct_val * 100
    # 无 user_id:按"分钟"分桶 —— 同分钟内多次请求得到相同结果(否则每次
    # 调用都拿到不同微秒数,等效随机分桶,失去稳定路由语义)。
    seed = utcnow().strftime("%Y%m%d%H%M")
    return _user_hash_bucket(seed, 10000) < pct_val * 100


def _match_gray_rule(
    rule: dict[str, Any],
    *,
    tenant_id: Optional[str],
    user_id: Optional[str],
    tags: Iterable[str] = (),
) -> bool:
    """判断单条灰度规则是否命中。所有维度 AND 关系，缺失维度跳过。"""
    # tenant 白名单
    if "tenant_id_in" in rule:
        if not tenant_id or tenant_id not in rule["tenant_id_in"]:
            return False
    # user 哈希分桶：``< N`` 即命中前 N% 用户
    if "user_id_hash_mod_100_lt" in rule:
        if not user_id:
            return False
        if _user_hash_bucket(user_id, 100) >= rule["user_id_hash_mod_100_lt"]:
            return False
    # 流量百分比
    if "traffic_pct" in rule:
        if not _match_traffic_pct(rule["traffic_pct"], user_id):
            return False
    # 时间窗
    if "time_range" in rule:
        if not _match_time_range(rule["time_range"]):
            return False
    # 标签匹配
    if "tag_in" in rule:
        if not set(rule["tag_in"]) & set(tags):
            return False
    return True


# ════════════════════════════════════════════════════════════════════════════
# 主类
# ════════════════════════════════════════════════════════════════════════════
class ConfigResolver:
    """五级覆盖 + 灰度路由解析器。

    输入数据形态（FileConfigCenter / MongoConfigCenter 共用）::

        defaults:           dict[category, {name, params, variants?}]
        global_overrides:   dict[category, {name, params, variants?}]
        tenant_overrides:   dict[tenant_id, dict[category, {name, params}]]
        user_overrides:     dict[user_id, dict[category, {name, params}]]
        request_override:   dict[category, {name, params}]
    """

    def __init__(self, *, overridable_at: dict[str, list[str]] | None = None) -> None:
        # 白名单：``{category: ["global", "tenant"]}`` 表示该 category 仅允许
        # 在 global / tenant 层覆盖（user / request 不准）。缺省允许全部层覆盖。
        # 用于保护安全敏感配置（如 auth_enabled、rate_limit）不被用户层修改。
        self._overridable_at = overridable_at or {}

    def resolve(
        self,
        category: str,
        *,
        defaults: dict[str, Any],
        global_overrides: dict[str, Any] | None = None,
        tenant_overrides: dict[str, Any] | None = None,
        user_overrides: dict[str, Any] | None = None,
        request_override: dict[str, Any] | None = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        tags: Iterable[str] = (),
    ) -> tuple[dict[str, Any], str]:
        """按五级 + 灰度规则解析。

        :returns: ``(merged_dict, source)``
                  ``merged_dict`` 形如 ``{"name": ..., "params": {...}, "variants": [...]}``；
                  ``source`` 是命中的最高优先层（default / global / tenant / user / request）
        """
        cur: dict[str, Any] = deepcopy(defaults.get(category, {}))
        source = "default"
        allowed = set(self._overridable_at.get(category, ["global", "tenant", "user", "request"]))

        # 1. global
        if "global" in allowed and global_overrides and category in global_overrides:
            cur = _deep_merge(cur, global_overrides[category])
            source = "global"

        # 2. tenant
        if (
            "tenant" in allowed
            and tenant_id
            and tenant_overrides
            and tenant_id in tenant_overrides
            and category in tenant_overrides[tenant_id]
        ):
            cur = _deep_merge(cur, tenant_overrides[tenant_id][category])
            source = "tenant"

        # 3. user
        if (
            "user" in allowed
            and user_id
            and user_overrides
            and user_id in user_overrides
            and category in user_overrides[user_id]
        ):
            cur = _deep_merge(cur, user_overrides[user_id][category])
            source = "user"

        # 4. request
        if "request" in allowed and request_override and category in request_override:
            cur = _deep_merge(cur, request_override[category])
            source = "request"

        # 5. 灰度变体（在合并完成后选择）
        cur = self._apply_variants(cur, tenant_id=tenant_id, user_id=user_id, tags=tags)

        return cur, source

    # ── 测试便利（公开静态方法） ──
    @staticmethod
    def match_user_hash(user_id: str, lt: int) -> bool:
        """便捷断言：user_id 是否落在前 ``lt%`` 用户内。"""
        return _user_hash_bucket(user_id, 100) < lt

    @staticmethod
    def hash_bucket(user_id: str, modulus: int = 100) -> int:
        """暴露内部哈希函数，便于自定义分桶测试。"""
        return _user_hash_bucket(user_id, modulus)

    # ── 灰度变体应用 ──
    def _apply_variants(
        self,
        cfg: dict[str, Any],
        *,
        tenant_id: Optional[str],
        user_id: Optional[str],
        tags: Iterable[str],
    ) -> dict[str, Any]:
        """灰度变体:在合并后的 cfg 上选择第一个命中的 variant 覆盖 name/params。

        cfg 形如(plugin entry)::

            {
              "name": "vector_milvus",
              "params": {...},
              "variants": [
                {"name": "vector_milvus_fr", "params": {...}, "match": {...}},
                ...
              ]
            }

        Prompt 简化语法糖 variant也被支持 —— variant 顶层带 prompt body
        字段(``template`` / ``variables`` 等)时自动包装到 ``params``::

            variants:
              - match: {tenant_id_in: ["acme"]}
                template: "Acme 版本..."         # ← 等价于 params.template

        命中后输出含 ``_variant_matched: true`` 标志,便于排查 / 监控。
        """
        variants = cfg.get("variants")
        if not variants:
            return cfg
        # 自上而下命中即停 —— 配置作者按优先级排序
        for v in variants:
            match = v.get("match", {})
            if _match_gray_rule(match, tenant_id=tenant_id, user_id=user_id, tags=tags):
                merged = deepcopy(cfg)
                merged.pop("variants", None)
                merged["name"] = v.get("name", merged.get("name"))

                # 收集 variant 的 params 覆盖:显式 params + Prompt 简化语法糖
                v_params: dict[str, Any] = {}
                if isinstance(v.get("params"), dict):
                    v_params = dict(v["params"])
                # variant 顶层的 prompt 字段自动归并到 params
                # (与 file_center._flatten_defaults 的展开规则对称)
                for sugar_key in ("template", "variables", "description", "version", "tags"):
                    if sugar_key in v:
                        v_params.setdefault(sugar_key, v[sugar_key])

                if v_params:
                    merged["params"] = _deep_merge(merged.get("params", {}), v_params)
                merged["_variant_matched"] = True
                return merged
        # 无任何 variant 命中:返回原配置(剥掉 variants 字段以保持下游消费简洁)
        out = deepcopy(cfg)
        out.pop("variants", None)
        return out


__all__ = ["ConfigResolver"]
