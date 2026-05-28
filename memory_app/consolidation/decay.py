"""DecayManager —— 被动衰减 + 容量约束。

═══════════════════════════════════════════════════════════════════════════════
被动衰减(``run_passive_decay``)
═══════════════════════════════════════════════════════════════════════════════
扫描用户所有 ``COLD`` 状态的 MemCell:
- ``trs_score < retention_threshold`` (默认 0.15)  → ARCHIVED
- ``age > archive_after_days``        (默认 90)   → ARCHIVED

返回归档数量。

═══════════════════════════════════════════════════════════════════════════════
容量约束(``enforce_capacity``)
═══════════════════════════════════════════════════════════════════════════════
扫描用户所有 MemCell:
- 总数 ≤ ``max_memories_per_user`` → 不操作,返回 0
- 否则按 FSFM ``score`` 从低到高,把最低的 N 条标记为 ARCHIVED;
  N = ``min(overflow, total × safety_margin)``(避免单轮删崩,默认 10%)

═══════════════════════════════════════════════════════════════════════════════
依赖
═══════════════════════════════════════════════════════════════════════════════
- ``mongo_repo``        实现 ``find_by_state`` / ``find_all`` / ``count`` / ``update``
- ``scorer``            实现 ``score_cell(cell, now)`` / ``trs_score(cell, now)``;
                        通常注入 :class:`memory_app.scoring.FSFMScorer`
                        (或 :class:`FSFM4DScorer` 插件 —— 鸭子类型即可)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from memory_app.internal_models import MemCell, MemoryState

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class DecayConfig:
    """DecayManager 参数。"""

    retention_threshold: float = 0.15
    archive_after_days: float = 90.0
    max_memories_per_user: int = 10000
    safety_margin: float = 0.10  # 单轮最多删 total × 10%


def parse_decay_config(params: dict[str, Any] | None) -> DecayConfig:
    cfg = DecayConfig()
    if not params:
        return cfg
    for k in (
        "retention_threshold",
        "archive_after_days",
        "max_memories_per_user",
        "safety_margin",
    ):
        if k in params:
            try:
                v: Any = params[k]
                if k in ("max_memories_per_user",):
                    setattr(cfg, k, int(v))
                else:
                    setattr(cfg, k, float(v))
            except (TypeError, ValueError):
                continue
    return cfg


# ════════════════════════════════════════════════════════════════════════════
# 主类
# ════════════════════════════════════════════════════════════════════════════
class DecayManager:
    """被动衰减 + 容量约束执行器。"""

    def __init__(
        self,
        mongo_repo: Any,
        scorer: Any,
        config: DecayConfig | None = None,
    ) -> None:
        self.mongo_repo = mongo_repo
        self.scorer = scorer
        self.config = config or DecayConfig()

    # ────────────────────────────────────────────────────────────────────────
    # 被动衰减
    # ────────────────────────────────────────────────────────────────────────
    async def run_passive_decay(
        self,
        tenant_id: str,
        user_id: str,
        now: datetime | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """扫描 COLD 记忆,满足条件的标记为 ARCHIVED。

        :returns: ``{archived_count, scanned_count, candidate_ids}``
        """
        n = now or _utcnow()
        try:
            cells = await self.mongo_repo.find_by_state(
                tenant_id, user_id, MemoryState.COLD
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("decay find_by_state failed: %s", e)
            return {"archived_count": 0, "scanned_count": 0, "candidate_ids": []}

        candidates: list[str] = []
        for cell in cells:
            if self._should_archive(cell, n):
                candidates.append(cell.mem_cell_id)

        archived = 0
        if not dry_run and candidates:
            archived = await self._bulk_archive(candidates, n)
        return {
            "archived_count": archived,
            "scanned_count": len(cells),
            "candidate_ids": candidates,
        }

    # ────────────────────────────────────────────────────────────────────────
    # 容量约束
    # ────────────────────────────────────────────────────────────────────────
    async def enforce_capacity(
        self,
        tenant_id: str,
        user_id: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        try:
            total = await self.mongo_repo.count(tenant_id, user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("decay count failed: %s", e)
            return {"archived_count": 0, "total": 0, "candidate_ids": []}

        if total <= self.config.max_memories_per_user:
            return {"archived_count": 0, "total": total, "candidate_ids": []}

        overflow = total - self.config.max_memories_per_user
        # 安全边际:单轮最多删 total × safety_margin
        cap = max(1, int(total * max(0.0, min(1.0, self.config.safety_margin))))
        target_n = min(overflow, cap)

        try:
            cells = await self.mongo_repo.find_all(tenant_id, user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("decay find_all failed: %s", e)
            return {"archived_count": 0, "total": total, "candidate_ids": []}

        n = _utcnow()
        scored = [
            (self._safe_score(cell, n), cell) for cell in cells
            # 已 ARCHIVED 的不再删
            if cell.state != MemoryState.ARCHIVED
        ]
        scored.sort(key=lambda t: t[0])
        candidates = [c.mem_cell_id for _, c in scored[:target_n]]

        archived = 0
        if not dry_run and candidates:
            archived = await self._bulk_archive(candidates, n)
        return {
            "archived_count": archived,
            "total": total,
            "overflow": overflow,
            "candidate_ids": candidates,
        }

    # ────────────────────────────────────────────────────────────────────────
    # 批量归档(repo 支持 bulk_set_state 时单次 round-trip;否则回退串行)
    # ────────────────────────────────────────────────────────────────────────
    async def _bulk_archive(
        self, mem_cell_ids: list[str], now: datetime
    ) -> int:
        bulk_fn = getattr(self.mongo_repo, "bulk_set_state", None)
        if callable(bulk_fn):
            try:
                affected = await bulk_fn(mem_cell_ids, MemoryState.ARCHIVED)
                return int(affected if affected is not None else len(mem_cell_ids))
            except Exception as e:  # noqa: BLE001
                logger.warning("bulk archive failed, fallback to per-id: %s", e)
        # 兼容仅实现 update 的旧 repo / fake
        archived = 0
        for mid in mem_cell_ids:
            try:
                await self.mongo_repo.update(
                    mid,
                    {
                        "state": MemoryState.ARCHIVED.value,
                        "updated_at": now,
                    },
                )
                archived += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("decay update failed for %s: %s", mid, e)
        return archived

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    def _should_archive(self, cell: MemCell, now: datetime) -> bool:
        # age > archive_after_days
        created = _normalize(cell.created_at) if cell.created_at else _normalize(now)
        age_days = max(0.0, (_normalize(now) - created).total_seconds() / 86400.0)
        if age_days > self.config.archive_after_days:
            return True
        # retention(由 trs_score 提供)< threshold
        try:
            r = self.scorer.trs_score(cell, now)
        except Exception:  # noqa: BLE001
            return False
        return r < self.config.retention_threshold

    def _safe_score(self, cell: MemCell, now: datetime) -> float:
        try:
            return float(self.scorer.score_cell(cell, now=now))
        except AttributeError:
            try:
                return float(self.scorer.score(cell, now=now))
            except Exception:  # noqa: BLE001
                return 0.0
        except Exception:  # noqa: BLE001
            return 0.0


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


__all__ = ["DecayManager", "DecayConfig", "parse_decay_config"]
