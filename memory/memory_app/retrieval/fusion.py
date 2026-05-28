"""RRF 融合 + 信号增强。

═══════════════════════════════════════════════════════════════════════════════
RRF 公式
═══════════════════════════════════════════════════════════════════════════════
::

    RRFScore_i = Σ_c w_c / (k + rank_c(i) + 1)

- ``w_c``  通道权重(默认  表;``entity`` / ``graph`` 通道未启用时权重缺省 0)
- ``rank_c(i)`` 该 memory 在通道 c 内的 rank(从 0 起)
- ``k=60`` 平滑常数

═══════════════════════════════════════════════════════════════════════════════
信号增强公式
═══════════════════════════════════════════════════════════════════════════════
::

    FinalScore_i = RRFScore_i × TimeDecay_i × (1 + Imp_i)

- ``TimeDecay`` 已经过  三因子衰减或 Langevin SDE,默认 1.0(无衰减)
- ``Imp``       重要性分数 [0, 1],默认 0.0(原样)

调用方负责喂入 ``time_decays`` / ``importances`` —— 本类不依赖具体 ForgettingPolicy。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from memory_app.internal_models import RankedMemory

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class RRFConfig:
    """RRF 配置。

    权重缺省(对应通道未启用 / 未配置):
    - 当前版本仅 BM25 + Vector → 实际生效权重为 ``{bm25: 0.40, vector: 0.60}``
      
    - 冷路径+ 全通道启用 → ``{bm25: 0.30, vector: 0.40, entity: 0.15, graph: 0.15}``

    构造默认值采用全通道版本;`fuse` 方法对未提供的通道权重视为 0。
    """

    k: int = 60
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "bm25": 0.30,
            "vector": 0.40,
            "entity": 0.15,
            "graph": 0.15,
        }
    )

    def weight(self, channel: str) -> float:
        return float(self.weights.get(channel, 0.0))


def parse_rrf_config(params: dict[str, Any] | None) -> RRFConfig:
    """从插件 params 字典构造 :class:`RRFConfig`。

    支持:
    - ``k`` (int)
    - ``weights`` (dict[str, float] —— 部分通道也行,缺省的 channel 权重 = 0)
    """
    cfg = RRFConfig()
    if not params:
        return cfg
    if "k" in params:
        cfg.k = int(params["k"])
    raw_w = params.get("weights")
    if isinstance(raw_w, dict):
        # 不要全替换,允许 user 只覆盖部分通道
        for k, v in raw_w.items():
            try:
                cfg.weights[k] = float(v)
            except (TypeError, ValueError):
                continue
    return cfg


# ════════════════════════════════════════════════════════════════════════════
# 信号增强工具(独立暴露,方便单测 + 复用)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class SignalBoost:
    """信号增强参数容器。"""

    time_decays: dict[str, float] = field(default_factory=dict)
    importances: dict[str, float] = field(default_factory=dict)

    def factor_for(self, memory_id: str) -> float:
        td = float(self.time_decays.get(memory_id, 1.0))
        imp = float(self.importances.get(memory_id, 0.0))
        return td * (1.0 + imp)


# ════════════════════════════════════════════════════════════════════════════
# 基类
# ════════════════════════════════════════════════════════════════════════════
class BaseFusion(ABC):
    """多路融合模板。"""

    async def fuse(
        self,
        channel_outputs: dict[str, list[RankedMemory]],
        weights: dict[str, float] | None = None,
    ) -> list[RankedMemory]:
        """合并 + 排名。

        :param channel_outputs: ``{channel_name: [RankedMemory, ...]}``
        :param weights:         临时覆盖默认权重;``None`` 用配置内权重
        """
        if not channel_outputs:
            return []
        merged = self._merge_channel_results(channel_outputs, weights)
        return self._finalize_ranking(merged)

    @abstractmethod
    def _merge_channel_results(
        self,
        channel_outputs: dict[str, list[RankedMemory]],
        weights: dict[str, float] | None,
    ) -> dict[str, RankedMemory]:
        """子类实现:把多通道结果合并去重 + 累计 score。"""

    def _finalize_ranking(
        self, hit_map: dict[str, RankedMemory]
    ) -> list[RankedMemory]:
        items = list(hit_map.values())
        items.sort(key=lambda h: h.score, reverse=True)
        for i, h in enumerate(items):
            h.rank = i
        return items


# ════════════════════════════════════════════════════════════════════════════
# RRF 融合
# ════════════════════════════════════════════════════════════════════════════
class RRFFusion(BaseFusion):
    """加权 Reciprocal Rank Fusion。"""

    def __init__(self, config: RRFConfig | None = None) -> None:
        self.config = config or RRFConfig()

    # ────────────────────────────────────────────────────────────────────────
    # 主合并
    # ────────────────────────────────────────────────────────────────────────
    def _merge_channel_results(
        self,
        channel_outputs: dict[str, list[RankedMemory]],
        weights: dict[str, float] | None,
    ) -> dict[str, RankedMemory]:
        scores: dict[str, float] = {}
        # 注意:同一 memory_id 可能在多通道出现,我们要保留**第一个见到**的副本作为
        # 输出载体(不修改入参,避免被并发改写),其它通道继续累计 score。
        out: dict[str, RankedMemory] = {}
        per_channel_sources: dict[str, list[str]] = {}

        for channel, hits in channel_outputs.items():
            w = self._weight_for(channel, weights)
            if w <= 0:
                continue
            for rank, h in enumerate(hits):
                if not h.memory_id:
                    continue
                contribution = w / (self.config.k + rank + 1)
                scores[h.memory_id] = scores.get(h.memory_id, 0.0) + contribution
                if h.memory_id not in out:
                    # 复制一份避免改写调用方对象。**shallow** 即可:本类只修改
                    # score/rank/metadata 三个字段,且下方 ``hit.metadata =
                    # dict(...)`` 已对 metadata 做新 dict 替换,不会回写到原对象。
                    # 深拷贝 RankedMemory(含 1024d embedding + content)代价昂贵,
                    # 热路径上 N=160 量级时省 N×deep-copy 开销显著。
                    out[h.memory_id] = h.model_copy()
                per_channel_sources.setdefault(h.memory_id, []).append(channel)

        # 写回 score + 多通道命中标注
        for mid, hit in out.items():
            hit.score = scores.get(mid, 0.0)
            hit.metadata = dict(hit.metadata or {})
            hit.metadata["rrf_score"] = hit.score
            hit.metadata["matched_channels"] = list(per_channel_sources.get(mid, []))
        return out

    # ────────────────────────────────────────────────────────────────────────
    # 信号增强
    # ────────────────────────────────────────────────────────────────────────
    def apply_signal_boost(
        self,
        hits: list[RankedMemory],
        time_decays: dict[str, float] | None = None,
        importances: dict[str, float] | None = None,
    ) -> list[RankedMemory]:
        """``FinalScore = RRFScore × TimeDecay × (1 + Imp)``。

        约定:
        - **就地**修改 hits 内每项 score(便于在 SignalBoostStage 内串联)
        - 返回的列表已按新 score 降序;``rank`` 重新填充
        - 缺失项采用默认值 ``td=1.0, imp=0.0``(等价不增强)
        """
        boost = SignalBoost(
            time_decays=time_decays or {},
            importances=importances or {},
        )
        for h in hits:
            h.score = float(h.score) * boost.factor_for(h.memory_id)
        hits.sort(key=lambda h: h.score, reverse=True)
        for i, h in enumerate(hits):
            h.rank = i
        return hits

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    def _weight_for(
        self, channel: str, override: dict[str, float] | None
    ) -> float:
        if override is not None and channel in override:
            try:
                return float(override[channel])
            except (TypeError, ValueError):
                return 0.0
        return self.config.weight(channel)


__all__ = [
    "RRFConfig",
    "parse_rrf_config",
    "SignalBoost",
    "BaseFusion",
    "RRFFusion",
]
