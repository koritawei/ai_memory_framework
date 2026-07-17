"""Phase 5 评分与强化公式(设计文档 §7.2 / §7.5)。

═══════════════════════════════════════════════════════════════════════════════
模块组织
═══════════════════════════════════════════════════════════════════════════════
- :class:`ReinforceConfig` + :func:`compute_strength_delta`
                        反馈强化公式(§7.5,Step 5.1)
- :class:`FSFMConfig`  + :class:`FSFMScorer`
                        FSFM 四维重要性评分(§7.2,Step 5.3)
- :class:`EbbinghausConfig` + :func:`ebbinghaus_retention`
                        艾宾浩斯衰减(§7.3,辅助 ForgettingPolicy 默认实现)

插件层 :mod:`memory_app.plugins_default.synaptic_reinforcer` /
:mod:`memory_app.plugins_default.fsfm_scorer` /
:mod:`memory_app.plugins_default.ebbinghaus_policy` 是这些核心算法的薄包装。

═══════════════════════════════════════════════════════════════════════════════
反馈 → strength 增量(设计文档 §7.5 / SPI Reinforcer 表)
═══════════════════════════════════════════════════════════════════════════════
::

    | feedback_type     | 默认 signal_value |
    | EXPLICIT_CONFIRM  | +1.0 |
    | POSITIVE          | +0.3 |
    | NEGATIVE          | -0.5 |
    | CORRECTION        | -2.0 |
    | DELETION_REQUEST  | -10.0 |

公式::

    S_new = clip(S_old + η × signal − λ × Δt_days, 0, S_max)

- η = 0.3   学习率
- λ = 0.01  日衰减(单位:1/天)
- S_max = 5.0
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from memory_app.internal_models import MemCell
from memory_app.schemas.feedback import FeedbackType


def _default_feedback_signals() -> dict[FeedbackType, float]:
    """默认 feedback_type → signal 增量(SPI 契约表)。

    抽成独立函数,让 :class:`ReinforceConfig` 用 ``field(default_factory=...)``
    符合 dataclass 最佳实践,避免可变默认参数反模式。
    """
    return {
        FeedbackType.EXPLICIT_CONFIRM: 1.0,
        FeedbackType.POSITIVE: 0.3,
        FeedbackType.NEGATIVE: -0.5,
        FeedbackType.CORRECTION: -2.0,
        FeedbackType.DELETION_REQUEST: -10.0,
    }


# ════════════════════════════════════════════════════════════════════════════
# Step 5.1:反馈强化
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class ReinforceConfig:
    """SynapticPlasticityReinforcer 的参数容器。"""

    eta: float = 0.3
    lambda_per_day: float = 0.01
    s_max: float = 5.0

    #: feedback_type → 默认 signal(signal_value=0 时填充)。
    #: 用 ``field(default_factory=...)`` 避免可变默认参数反模式
    #: (原版 ``= None  # type: ignore`` + ``__post_init__`` 兜底)。
    default_signals: dict[FeedbackType, float] = field(
        default_factory=_default_feedback_signals
    )


def parse_reinforce_config(params: dict[str, Any] | None) -> ReinforceConfig:
    cfg = ReinforceConfig()
    if not params:
        return cfg
    if "eta" in params:
        cfg.eta = float(params["eta"])
    if "lambda_per_day" in params:
        cfg.lambda_per_day = float(params["lambda_per_day"])
    if "s_max" in params:
        cfg.s_max = float(params["s_max"])
    raw = params.get("default_signals")
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                ft = FeedbackType(str(k).lower())
                cfg.default_signals[ft] = float(v)
            except (ValueError, TypeError):
                continue
    return cfg


def resolve_signal(
    feedback_type: FeedbackType,
    signal_value: float,
    config: ReinforceConfig,
) -> float:
    """``signal_value=0.0`` 时按表填充默认值;否则原样返回。"""
    if signal_value:
        return float(signal_value)
    return float(config.default_signals.get(feedback_type, 0.0))


def compute_strength_delta(
    *,
    old_strength: float,
    signal: float,
    last_at: datetime | None,
    now: datetime,
    config: ReinforceConfig,
) -> tuple[float, float]:
    """计算新的 strength 与 delta。

    - 时间衰减项:`λ × Δt_days`,``last_at`` 缺失时取 0(无衰减)
    - clip 到 ``[0, s_max]``

    :returns: ``(new_strength, delta)`` —— delta 可正可负
    """
    if last_at is None:
        dt_days = 0.0
    else:
        dt = _normalize(now) - _normalize(last_at)
        dt_days = max(0.0, dt.total_seconds() / 86400.0)
    raw = float(old_strength) + config.eta * float(signal) - config.lambda_per_day * dt_days
    new_strength = max(0.0, min(config.s_max, raw))
    return new_strength, new_strength - float(old_strength)


# ════════════════════════════════════════════════════════════════════════════
# Step 5.3:FSFM 四维重要性
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class FSFMConfig:
    """FSFM 权重 / 半衰期。

    设计文档 §7.2 标注的子分数定域(CQA/BVE [0,3], TRS [0,2], SRC [-10,0])使
    composite 落在 [-1.5, 2.25];Step 5.3 的简化版把所有子分归一到 [0, 1]
    并用线性加权,落在 [0, 1] —— 便于 RetrievalPipeline 的信号增强直接消费。
    """

    w_cqa: float = 0.25
    w_bve: float = 0.30
    w_trs: float = 0.25
    w_src: float = 0.20
    trs_half_life_days: float = 30.0


def parse_fsfm_config(params: dict[str, Any] | None) -> FSFMConfig:
    cfg = FSFMConfig()
    if not params:
        return cfg
    for k in ("w_cqa", "w_bve", "w_trs", "w_src", "trs_half_life_days"):
        if k in params:
            try:
                setattr(cfg, k, float(params[k]))
            except (TypeError, ValueError):
                continue
    return cfg


class FSFMScorer:
    """四维评分(归一化版本,落在 [0, 1])。"""

    def __init__(self, config: FSFMConfig | None = None) -> None:
        self.config = config or FSFMConfig()

    # ────────────────────────────────────────────────────────────────────────
    # Public
    # ────────────────────────────────────────────────────────────────────────
    def score(self, cell: MemCell, now: datetime | None = None) -> float:
        cqa = self.cqa_score(cell)
        bve = self.bve_score(cell)
        trs = self.trs_score(cell, now or _utcnow())
        src = self.src_score(cell)
        return self._composite(cqa, bve, trs, src)

    def detail(self, cell: MemCell, now: datetime | None = None) -> dict[str, float]:
        """返回各维度子分;便于排查与离线分析。

        旧实现复用 ``score(cell)`` 让 cqa/bve/trs/src 各算两次;Phase 5 反馈与
        Phase 6 巩固高频路径直接调 ``detail``,double computation 实测占可观 CPU。
        现在 4 个子分各算 1 次,composite 复用本地变量。
        """
        n = now or _utcnow()
        cqa = self.cqa_score(cell)
        bve = self.bve_score(cell)
        trs = self.trs_score(cell, n)
        src = self.src_score(cell)
        return {
            "cqa": cqa,
            "bve": bve,
            "trs": trs,
            "src": src,
            "composite": self._composite(cqa, bve, trs, src),
        }

    def _composite(self, cqa: float, bve: float, trs: float, src: float) -> float:
        """加权综合 4 子分。提取为方法让 score() / detail() 共用同一算式。"""
        return round(
            self.config.w_cqa * cqa
            + self.config.w_bve * bve
            + self.config.w_trs * trs
            + self.config.w_src * src,
            6,
        )

    # ────────────────────────────────────────────────────────────────────────
    # 子分(归一到 [0, 1])
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def cqa_score(cell: MemCell) -> float:
        """上下文质量:文本长度 / 信息密度。``len(text) / 500`` 截断到 1.0。"""
        text_len = len(cell.text or "")
        return float(min(text_len / 500.0, 1.0))

    @staticmethod
    def bve_score(cell: MemCell) -> float:
        """行为价值:``access_count × 0.2 + strength × 0.1``,截断到 1.0。"""
        return float(min(cell.access_count * 0.2 + cell.strength * 0.1, 1.0))

    def trs_score(self, cell: MemCell, now: datetime) -> float:
        """时间衰减:指数半衰期。

        ``exp(-ln(2) × age_days / half_life)``
        """
        created = _normalize(cell.created_at) if cell.created_at else _normalize(now)
        age_days = max(0.0, (_normalize(now) - created).total_seconds() / 86400.0)
        if self.config.trs_half_life_days <= 0:
            return 1.0
        return float(math.exp(-0.6931471805599453 * age_days / self.config.trs_half_life_days))

    @staticmethod
    def src_score(cell: MemCell) -> float:
        """语义丰富度:``len(raw_data_ids) × 0.3``,截断到 1.0。"""
        return float(min(len(cell.raw_data_ids or []) * 0.3, 1.0))


# ════════════════════════════════════════════════════════════════════════════
# 艾宾浩斯衰减(辅助 ForgettingPolicy 默认实现)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class EbbinghausConfig:
    """艾宾浩斯遗忘曲线参数。

    ::

        retention = exp(-Δt_days / S × strength_factor) × access_factor

    - ``S=4.0`` 半衰期(天),strength=1 时
    - access_count 加成:``min(1, access_count / 10)``,刚记的高频记忆更稳
    - 状态加成:ACTIVE 1.0,WARM 0.85,COLD 0.6,ARCHIVED 0.2
    """

    s_base: float = 4.0
    threshold_forget: float = 0.15


def parse_ebbinghaus_config(params: dict[str, Any] | None) -> EbbinghausConfig:
    cfg = EbbinghausConfig()
    if not params:
        return cfg
    if "s_base" in params:
        try:
            cfg.s_base = float(params["s_base"])
        except (TypeError, ValueError):
            pass
    if "threshold_forget" in params:
        try:
            cfg.threshold_forget = float(params["threshold_forget"])
        except (TypeError, ValueError):
            pass
    return cfg


def ebbinghaus_retention(
    *,
    age_days: float,
    strength: float,
    access_count: int,
    config: EbbinghausConfig,
) -> float:
    """艾宾浩斯保留度,落在 [0, 1]。

    :param age_days: ``now - created_at`` 天数(已归一化)
    """
    if age_days < 0:
        age_days = 0.0
    s = max(0.1, config.s_base * max(0.1, float(strength)))
    base = math.exp(-age_days / s)
    access_factor = min(1.0, max(0.0, float(access_count)) / 10.0)
    # access_factor 给一个最小底数避免新记忆 access=0 直接归零
    return float(base * (0.6 + 0.4 * access_factor))


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


__all__ = [
    "ReinforceConfig",
    "parse_reinforce_config",
    "resolve_signal",
    "compute_strength_delta",
    "FSFMConfig",
    "parse_fsfm_config",
    "FSFMScorer",
    "EbbinghausConfig",
    "parse_ebbinghaus_config",
    "ebbinghaus_retention",
]
