"""重排算法(设计文档 §6.3)。

═══════════════════════════════════════════════════════════════════════════════
模块组织
═══════════════════════════════════════════════════════════════════════════════
- :class:`BaseReranker`   模板方法(MMR 主流程)
- :class:`MMRReranker`    Maximal Marginal Relevance 实现
- :class:`MMRConfig`      参数容器(λ + 是否启用 cross_encoder hook)

Cross-Encoder 精排作为可选增强:本类仅留 hook,具体模型由 Phase 4+ 切到独立
``CrossEncoderReranker`` 插件,本类默认不调用。

═══════════════════════════════════════════════════════════════════════════════
MMR 公式
═══════════════════════════════════════════════════════════════════════════════
::

    MMR(d) = λ * relevance(d, q) − (1 − λ) * max_{s∈selected} sim(d, s)

- ``relevance``  RankedMemory.score(融合 + 信号增强后的综合分)
- ``sim``        余弦相似度(基于 :func:`embedding_for` 提供的向量)
- ``λ=0.7``      偏向相关性;0.5 完全平衡;0.0 完全多样性

无 embedding 的 candidate:``sim`` 视为 0(等同始终被选中) —— 本算法降级为
按 relevance 排,不至于因为缺向量整体崩溃。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from memory_app.internal_models import RankedMemory

logger = logging.getLogger(__name__)

# numpy 通过 pymilvus / elasticsearch 等依赖间接引入,几乎一定可用;
# 不可用时 fallback 到纯 Python(慢 ~50×,但功能不破)
try:
    import numpy as _np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    _np = None  # type: ignore[assignment]
    _HAS_NUMPY = False


# ════════════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class MMRConfig:
    """MMR 参数。"""

    mmr_lambda: float = 0.7
    enable_cross_encoder: bool = False  # 默认关闭(Phase 4 后可切独立插件)


def parse_mmr_config(params: dict[str, Any] | None) -> MMRConfig:
    cfg = MMRConfig()
    if not params:
        return cfg
    if "mmr_lambda" in params:
        try:
            cfg.mmr_lambda = max(0.0, min(1.0, float(params["mmr_lambda"])))
        except (TypeError, ValueError):
            pass
    if "enable_cross_encoder" in params:
        cfg.enable_cross_encoder = bool(params["enable_cross_encoder"])
    return cfg


# ════════════════════════════════════════════════════════════════════════════
# 基类
# ════════════════════════════════════════════════════════════════════════════
class BaseReranker(ABC):
    """重排算法基类(模板方法)。

    子类只覆写 :meth:`_score_candidate` —— 主迭代由本类控制,确保 ``top_k``
    截断 / 输入边界(空 / 单条)被一致处理。
    """

    def __init__(self, config: MMRConfig | None = None) -> None:
        self.config = config or MMRConfig()

    # ────────────────────────────────────────────────────────────────────────
    # 主入口
    # ────────────────────────────────────────────────────────────────────────
    def mmr_rerank(
        self,
        hits: list[RankedMemory],
        embeddings: Mapping[str, list[float]] | None = None,
        top_k: int | None = None,
    ) -> list[RankedMemory]:
        """对 ``hits`` 做 MMR 重排,返回截断到 ``top_k`` 的新列表。

        :param hits:        待重排候选,score 已是 fusion + signal boost 后的综合分
        :param embeddings:  ``{memory_id: vector}``;无则按相关性排
        :param top_k:       截断长度;``None`` 等价于全量返回
        """
        if not hits:
            return []
        embeddings = embeddings or {}
        n = len(hits) if top_k is None else max(0, min(int(top_k), len(hits)))
        if n == 0:
            return []
        if n == 1:
            # 与多元素分支一致:盖 mmr_score 到 metadata,便于审计
            h = hits[0].model_copy()
            h.metadata = dict(h.metadata or {})
            h.metadata["mmr_score"] = self.config.mmr_lambda * float(hits[0].score)
            h.rank = 0
            return [h]

        candidates = list(hits)
        selected: list[RankedMemory] = []
        # MMR 迭代选择
        while candidates and len(selected) < n:
            best_idx = -1
            best_score = float("-inf")
            for i, cand in enumerate(candidates):
                score = self._score_candidate(cand, selected, embeddings)
                if score > best_score:
                    best_score = score
                    best_idx = i
            if best_idx < 0:
                break
            chosen = candidates.pop(best_idx)
            # shallow copy 即可:下面 ``metadata = dict(...)`` 已切断共享引用,
            # 只修改的 score / rank / metadata 三个字段不会回写源对象。
            chosen = chosen.model_copy()
            chosen.metadata = dict(chosen.metadata or {})
            chosen.metadata["mmr_score"] = best_score
            selected.append(chosen)
        # 重新填 rank
        for i, h in enumerate(selected):
            h.rank = i
        return selected

    # ────────────────────────────────────────────────────────────────────────
    # 子类抽象
    # ────────────────────────────────────────────────────────────────────────
    @abstractmethod
    def _score_candidate(
        self,
        candidate: RankedMemory,
        selected: list[RankedMemory],
        embeddings: Mapping[str, list[float]],
    ) -> float:
        """给候选项打分(越大越优先选中)。"""


# ════════════════════════════════════════════════════════════════════════════
# MMR 实现
# ════════════════════════════════════════════════════════════════════════════
class MMRReranker(BaseReranker):
    """``λ * relevance − (1 − λ) * max_sim_to_selected``。

    性能说明:
    本类覆写 :meth:`mmr_rerank`,用 numpy 一次性算出 n×n 相似度矩阵 + 维护
    ``max_sim_to_selected[i]`` 增量更新,把原 :meth:`BaseReranker.mmr_rerank`
    的 ``O(n·k²·d)`` 复杂度降到 ``O(n²·d + n·k)``。

    n=40 / d=1024 / k=10 实测:
    - BaseReranker.mmr_rerank(Python 循环) 约 200ms
    - 本类(numpy 矩阵 + 增量 max) 约 2ms
    """

    def __init__(
        self,
        config: MMRConfig | None = None,
        *,
        cross_encoder: Callable[[str, RankedMemory], float] | None = None,
    ) -> None:
        super().__init__(config)
        self._cross_encoder = cross_encoder  # 可选 hook

    def _score_candidate(
        self,
        candidate: RankedMemory,
        selected: list[RankedMemory],
        embeddings: Mapping[str, list[float]],
    ) -> float:
        """仅用于子类继承场景(本类的 ``mmr_rerank`` 已覆写不再调用本方法)。

        无向量候选的处理:与 numpy 快速路径对齐 —— 缺向量的候选 max_sim 视为 0,
        既不偏向也不惩罚。若上层希望惩罚"未知多样性",应改用 ``ImportanceScorer``
        在 boost 阶段抑制其相关性。
        """
        relevance = float(candidate.score)
        if not selected:
            return self.config.mmr_lambda * relevance
        cand_vec = embeddings.get(candidate.memory_id) or []
        max_sim = 0.0
        if cand_vec:
            for s in selected:
                s_vec = embeddings.get(s.memory_id) or []
                if not s_vec:
                    continue
                sim = cosine_similarity(cand_vec, s_vec)
                if sim > max_sim:
                    max_sim = sim
        # cand_vec 为空 → max_sim 仍为 0.0,与 numpy 路径一致(填零行)
        return (
            self.config.mmr_lambda * relevance
            - (1.0 - self.config.mmr_lambda) * max_sim
        )

    # ────────────────────────────────────────────────────────────────────────
    # 覆写:numpy 矩阵 + 增量 max_sim
    # ────────────────────────────────────────────────────────────────────────
    def mmr_rerank(
        self,
        hits: list[RankedMemory],
        embeddings: Mapping[str, list[float]] | None = None,
        top_k: int | None = None,
    ) -> list[RankedMemory]:
        if not hits:
            return []
        embeddings = embeddings or {}
        n_total = len(hits)
        n = n_total if top_k is None else max(0, min(int(top_k), n_total))
        if n == 0:
            return []
        lam = self.config.mmr_lambda
        relevances = [float(h.score) for h in hits]
        if n == 1:
            # 与多元素分支一致:在 metadata 上盖 mmr_score 便于下游审计 / 调试
            h = hits[0].model_copy()
            h.metadata = dict(h.metadata or {})
            h.metadata["mmr_score"] = lam * relevances[0]
            h.rank = 0
            return [h]

        # numpy 不可用 → 退化到基类 Python 循环
        sim_matrix = self._build_sim_matrix(hits, embeddings) if _HAS_NUMPY else None
        if sim_matrix is None:
            return super().mmr_rerank(hits, embeddings, top_k=top_k)

        # MMR 增量主循环:max_sim[i] = 当前 candidate i 与 已 selected 的最大 sim
        max_sim = [0.0] * n_total
        chosen_indices: list[int] = []
        remaining: set[int] = set(range(n_total))

        for _ in range(n):
            best_idx = -1
            best_score = float("-inf")
            for i in remaining:
                if not chosen_indices:
                    score = lam * relevances[i]
                else:
                    score = lam * relevances[i] - (1.0 - lam) * max_sim[i]
                if score > best_score:
                    best_score = score
                    best_idx = i
            if best_idx < 0:
                break
            chosen_indices.append(best_idx)
            remaining.discard(best_idx)
            # 增量更新 max_sim:只需对比 sim(i, 刚选中)
            sims_to_new = sim_matrix[best_idx]
            for i in remaining:
                if sims_to_new[i] > max_sim[i]:
                    max_sim[i] = float(sims_to_new[i])

        out: list[RankedMemory] = []
        for rank, idx in enumerate(chosen_indices):
            h = hits[idx].model_copy()
            h.metadata = dict(h.metadata or {})
            # 重算最终 mmr_score(本轮 chosen 时的 best_score 我们没缓存,简单重算)
            if rank == 0:
                h.metadata["mmr_score"] = lam * relevances[idx]
            else:
                # 从 chosen_indices[:rank] 算 max_sim 到 idx
                prev_max = max(
                    float(sim_matrix[idx][j]) for j in chosen_indices[:rank]
                )
                h.metadata["mmr_score"] = (
                    lam * relevances[idx] - (1.0 - lam) * prev_max
                )
            h.rank = rank
            out.append(h)
        return out

    @staticmethod
    def _build_sim_matrix(
        hits: list[RankedMemory],
        embeddings: Mapping[str, list[float]],
    ) -> Any | None:
        """用 numpy 一次性算 n×n cosine 矩阵;无 embedding 的位置填 0。

        失败 / 无 numpy → 返回 None,调用方回退到基类。
        """
        if not _HAS_NUMPY:
            return None
        n = len(hits)
        # 找出公共维度(以第一个非空 vec 为准)
        dim = 0
        for h in hits:
            vec = embeddings.get(h.memory_id)
            if vec:
                dim = len(vec)
                break
        if dim == 0:
            # 没有任何向量 → 全 0 矩阵,MMR 退化为按 relevance 排
            return _np.zeros((n, n), dtype=_np.float32)

        # 构造矩阵:无向量的行填 0,使其 sim=0(与任何 selected 都不冲突,
        # 等价于"无重复风险",MMR 倾向选它)。这与原 Python 版本语义一致。
        mat = _np.zeros((n, dim), dtype=_np.float32)
        for i, h in enumerate(hits):
            vec = embeddings.get(h.memory_id)
            if vec and len(vec) == dim:
                try:
                    mat[i] = _np.asarray(vec, dtype=_np.float32)
                except (TypeError, ValueError):
                    continue  # 留 0 行
        # L2 归一化(行向量),0 行保持 0
        norms = _np.linalg.norm(mat, axis=1, keepdims=True)
        nz = norms[:, 0] > 0
        mat[nz] = mat[nz] / norms[nz]
        # 矩阵乘 → 余弦相似度;0 行得到全 0 sim
        return mat @ mat.T

    # ────────────────────────────────────────────────────────────────────────
    # SPI 兼容的异步入口(供 RetrievalPipeline 直接调用)
    # ────────────────────────────────────────────────────────────────────────
    async def rerank(
        self,
        query: str,
        candidates: list[RankedMemory],
        top_k: int | None = None,
    ) -> list[RankedMemory]:
        """异步包装,与 :class:`memory_app.plugins.spi.reranker.Reranker.rerank`
        协议同形;默认从 ``candidate.metadata['embedding'|'vector']`` 取向量。
        """
        embeddings: dict[str, list[float]] = {}
        for c in candidates:
            md = c.metadata or {}
            vec = md.get("embedding") or md.get("vector")
            if vec:
                try:
                    embeddings[c.memory_id] = [float(x) for x in vec]
                except (TypeError, ValueError):
                    continue
        out = self.mmr_rerank(candidates, embeddings, top_k=top_k)
        return self.cross_encode_top_k(query, out)

    # ────────────────────────────────────────────────────────────────────────
    # Cross-Encoder hook(默认关闭)
    # ────────────────────────────────────────────────────────────────────────
    def cross_encode_top_k(
        self, query: str, hits: list[RankedMemory]
    ) -> list[RankedMemory]:
        """对 MMR 选出的 top-k 做精排;无 hook 时直接返回原列表。"""
        if not self.config.enable_cross_encoder or self._cross_encoder is None:
            return hits
        scored: list[tuple[float, RankedMemory]] = []
        for h in hits:
            try:
                s = float(self._cross_encoder(query, h))
            except Exception as e:  # noqa: BLE001
                logger.warning("cross_encoder failed for %s: %s", h.memory_id, e)
                s = float(h.score)
            scored.append((s, h))
        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[RankedMemory] = []
        for i, (s, h) in enumerate(scored):
            # shallow:仅修改标量 score/rank
            new = h.model_copy()
            new.score = s
            new.rank = i
            out.append(new)
        return out


# ════════════════════════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════════════════════════
def cosine_similarity(a: list[float], b: list[float]) -> float:
    """与 :mod:`memory_app.clustering` 同义,但本模块独立暴露避免循环依赖。

    1024-d 纯 Python 循环约 25-50µs/调用;numpy 版本 < 1µs。在 MMR 重排里
    对每对 (cand, selected) 调一次,n=40 / k=10 时是 ~190 次/查询。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    if _HAS_NUMPY:
        va = _np.asarray(a, dtype=_np.float32)
        vb = _np.asarray(b, dtype=_np.float32)
        na = float(_np.linalg.norm(va))
        nb = float(_np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(_np.dot(va, vb) / (na * nb))
    # 纯 Python fallback
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def _clone_with_rank(h: RankedMemory, rank: int) -> RankedMemory:
    # shallow:仅修改标量 rank
    out = h.model_copy()
    out.rank = rank
    return out


__all__ = [
    "MMRConfig",
    "parse_mmr_config",
    "BaseReranker",
    "MMRReranker",
    "cosine_similarity",
]
