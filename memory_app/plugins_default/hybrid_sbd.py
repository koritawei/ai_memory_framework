"""``hybrid_sbd`` —— Phase 3 Step 3.1 主推 SBD 插件:规则优先 + LLM 兜底。

═══════════════════════════════════════════════════════════════════════════════
策略
═══════════════════════════════════════════════════════════════════════════════
1. 走 :class:`memory_app.sbd.detect_boundaries` 得到候选 segment 列表
2. 对每个 segment 检查 :func:`memory_app.sbd.needs_llm_refinement`
3. 命中则把该段编号化送入 LLM(``sbd_llm_refine`` prompt),按 LLM 返回的
   ``boundary_index`` 二次切分
4. 任意环节失败 → fallback 原规则结果(不抛异常)

═══════════════════════════════════════════════════════════════════════════════
为什么要"组合插件"
═══════════════════════════════════════════════════════════════════════════════
- ``rule_sbd`` 在群聊高频场景内成本极低但可能错切 / 漏切大段
- ``llm_sbd`` 质量好但延迟 / 成本高
- ``hybrid_sbd`` 让规则先做 80%,只在剩余 20% 复杂边界上调 LLM

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    # 规则参数(等同 rule_sbd)
    time_gap_min:        int 默认 30
    max_window_size:     int 默认 20  (兼容 max_window_turns)
    max_window_tokens:   int 默认 512

    # LLM 兜底
    llm_fallback:        bool 默认 true
    refine_threshold:    int  默认 10  (segment turns > 此值触发 LLM)
    prompt_id:           str  默认 "sbd_llm_refine"

LLM 客户端通过 :meth:`bind_llm_client` 注入(测试 / 装配代码);
ConfigCenter 启动期 ``params`` 不含 client。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from memory_app.internal_models import RawData
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.boundary_detector import (
    BoundaryContext,
    BoundaryDetectionResult,
    BoundaryDetector,
)
from memory_app.prompt_runtime import get_prompt_manager
from memory_app.sbd import (
    LLM_REFINE_TURNS_THRESHOLD,
    SBDConfig,
    detect_boundaries,
    format_numbered_segments,
    needs_llm_refinement,
    parse_llm_boundary_response,
    parse_sbd_config,
    should_split,
    split_segment_at,
)

logger = logging.getLogger(__name__)


@register
class HybridSBD(BoundaryDetector):
    """规则优先 + LLM 兜底的 SBD 插件(Phase 3 默认)。"""

    meta = PluginMeta(
        name="hybrid_sbd",
        category="memory.generation.boundary_detector",
        version="1.0.0",
        description="规则优先 + LLM 兜底(prompt=sbd_llm_refine)",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "time_gap_min": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 30},
                "max_window_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "max_window_turns": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_window_tokens": {"type": "integer", "minimum": 1, "default": 512},
                "llm_fallback": {"type": "boolean", "default": True},
                "refine_threshold": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 200,
                    "default": LLM_REFINE_TURNS_THRESHOLD,
                },
                "prompt_id": {"type": "string", "default": "sbd_llm_refine"},
            },
        },
    )

    def __init__(self) -> None:
        self._config: SBDConfig = SBDConfig()
        self._llm_fallback: bool = True
        self._refine_threshold: int = LLM_REFINE_TURNS_THRESHOLD
        self._prompt_id: str = "sbd_llm_refine"
        self._llm_client: Any = None
        self._llm_calls: int = 0
        self._llm_failures: int = 0

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        cfg = dict(config)
        self._config = parse_sbd_config(cfg)
        self._llm_fallback = bool(cfg.get("llm_fallback", True))
        self._refine_threshold = int(
            cfg.get("refine_threshold", LLM_REFINE_TURNS_THRESHOLD)
        )
        self._prompt_id = str(cfg.get("prompt_id", "sbd_llm_refine"))
        logger.info(
            "hybrid_sbd started: rule(time_gap=%s, turns=%d, tokens=%d), "
            "llm_fallback=%s threshold=%d prompt=%s",
            self._config.time_gap_threshold, self._config.max_window_turns,
            self._config.max_window_tokens,
            self._llm_fallback, self._refine_threshold, self._prompt_id,
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"hybrid_sbd: rule_only={not self._llm_fallback}, "
                f"client={'bound' if self._llm_client is not None else 'unbound'}, "
                f"llm_calls={self._llm_calls}, llm_failures={self._llm_failures}"
            ),
        }

    async def metrics(self) -> dict:
        return {
            "hybrid_sbd_llm_calls": self._llm_calls,
            "hybrid_sbd_llm_failures": self._llm_failures,
        }

    # ────────────────────────────────────────────────────────────────────────
    # 注入 LLM client(由 deps / 测试装配)
    # ────────────────────────────────────────────────────────────────────────
    def bind_llm_client(self, client: Any) -> None:
        """运行时绑定 LLMProvider(``await client.generate(prompt) -> str``)。"""
        self._llm_client = client

    # ────────────────────────────────────────────────────────────────────────
    # SPI:单步判定(规则即可,不调 LLM)
    # ────────────────────────────────────────────────────────────────────────
    async def detect(
        self,
        history: list[RawData],
        new: list[RawData],
        ctx: BoundaryContext,
    ) -> BoundaryDetectionResult:
        """单步判定走规则路径(LLM 仅用于批量精修)。"""
        if not history:
            return BoundaryDetectionResult(
                should_end=False, should_wait=False, reasoning="cold_start", confidence=1.0
            )
        if not new:
            return BoundaryDetectionResult(
                should_end=False, should_wait=False, reasoning="empty_new", confidence=1.0
            )
        end, reason = should_split(history, new[0], self._config)
        return BoundaryDetectionResult(
            should_end=end, should_wait=False, reasoning=reason, confidence=1.0
        )

    # ────────────────────────────────────────────────────────────────────────
    # 批量切分:规则优先 + LLM 二次精修
    # ────────────────────────────────────────────────────────────────────────
    async def segment(self, raw_data_list: list[RawData]) -> list[list[RawData]]:
        """规则切分 → 必要时 LLM 兜底精修 → 返回最终 segment 列表。

        失败安全语义:LLM 任意环节抛异常,直接返回规则结果。
        """
        rule_segments = detect_boundaries(raw_data_list, self._config)
        if (
            not self._llm_fallback
            or self._llm_client is None
            or not needs_llm_refinement(rule_segments, turns_threshold=self._refine_threshold)
        ):
            return rule_segments

        refined: list[list[RawData]] = []
        for seg in rule_segments:
            if len(seg) <= self._refine_threshold:
                refined.append(seg)
                continue
            try:
                pieces = await self._llm_refine_segment(seg)
            except Exception as e:  # noqa: BLE001
                # 安全回退:整段保留
                self._llm_failures += 1
                logger.warning(
                    "hybrid_sbd llm refine failed (fallback to rule): %s", e
                )
                refined.append(seg)
                continue
            refined.extend(pieces)
        return [s for s in refined if s]

    async def _llm_refine_segment(self, segment: list[RawData]) -> list[list[RawData]]:
        """对单 segment 询问 LLM,返回精细切分(可能仍是 1 段)。"""
        prompt = await get_prompt_manager().render_for(
            self._prompt_id,
            tenant_id=segment[0].tenant_id,
            user_id=segment[0].user_id,
            numbered_text=format_numbered_segments(segment),
        )
        self._llm_calls += 1
        resp = await self._llm_client.generate(prompt)
        idx, reason, conf = parse_llm_boundary_response(resp)
        logger.debug(
            "hybrid_sbd refine: idx=%d, reason=%s, conf=%.2f", idx, reason, conf
        )
        if idx <= 0 or idx >= len(segment):
            # LLM 判同一话题(idx=-1)或取值越界 → 不切
            return [segment]
        return split_segment_at(segment, idx)


__all__ = ["HybridSBD"]
