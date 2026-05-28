"""``llm_sbd`` —— 冷路径 LLM 模式 SBD 插件。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
"纯 LLM" 切分实现:对整批 ``RawData`` 用 ``sbd_llm_refine`` prompt 询问
``boundary_index``,然后在该位置二次切分。冷路径 一般**不直接**用本插件;
正式落地是 ``hybrid_sbd``(规则优先 + 仅在需要时调本类的 ``_llm_refine``)。

═══════════════════════════════════════════════════════════════════════════════
为什么仍要登记本插件
═══════════════════════════════════════════════════════════════════════════════
- 评测对照实验:对比"纯规则" vs "纯 LLM" vs "混合" 三种切分质量
- ConfigCenter 灰度:在 incident 期临时把 ``boundary_detector.name`` 切到
  ``llm_sbd`` 强制走 LLM,排查规则是否过激

═══════════════════════════════════════════════════════════════════════════════
配置
═══════════════════════════════════════════════════════════════════════════════
::

    prompt_id:        str   默认 "sbd_llm_refine"
    max_window_turns: int   默认 50  (硬上限,防止 prompt 过长)
    failure_segments: int   默认 1   (LLM 异常时 fallback 输出几段)
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
    format_numbered_segments,
    parse_llm_boundary_response,
    split_segment_at,
)

logger = logging.getLogger(__name__)


@register
class LLMSBD(BoundaryDetector):
    """冷路径 —— 纯 LLM 切分(配合 ``sbd_llm_refine`` prompt)。"""

    meta = PluginMeta(
        name="llm_sbd",
        category="memory.generation.boundary_detector",
        version="1.0.0",
        description="LLM 模式 SBD(prompt=sbd_llm_refine);冷路径 / 灰度对照",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "prompt_id": {"type": "string", "default": "sbd_llm_refine"},
                "max_window_turns": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 200,
                    "default": 50,
                },
            },
        },
    )

    def __init__(self) -> None:
        self._prompt_id: str = "sbd_llm_refine"
        self._max_window_turns: int = 50
        # llm_provider 通过 :meth:`bind_llm_client` 注入;支持任意鸭子类型 ``await llm.generate(prompt)``
        self._llm_client: Any = None

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        self._prompt_id = str(config.get("prompt_id", "sbd_llm_refine"))
        self._max_window_turns = int(config.get("max_window_turns", 50))
        # llm_client 通常由组合插件(hybrid_sbd)或测试 fixture 注入;
        # ConfigCenter 启动期的 ``params`` 不包含 client 实例 —— 真正调用时若仍为 None 直接走 fallback
        logger.info(
            "llm_sbd started: prompt=%s, max_turns=%d", self._prompt_id, self._max_window_turns
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok" if self._llm_client is not None else "degraded",
            "detail": (
                f"llm_sbd: prompt={self._prompt_id}, "
                f"client={'bound' if self._llm_client is not None else 'unbound'}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # 注入 LLM client(运行时可由组合插件 / 测试调用)
    # ────────────────────────────────────────────────────────────────────────
    def bind_llm_client(self, client: Any) -> None:
        """绑定 LLMProvider 实例。

        支持鸭子类型:任何 ``await client.generate(prompt) -> str`` 即可。
        """
        self._llm_client = client

    # ────────────────────────────────────────────────────────────────────────
    # SPI: 单步判定(对外 detect 接口;批量切分走 segment)
    # ────────────────────────────────────────────────────────────────────────
    async def detect(
        self,
        history: list[RawData],
        new: list[RawData],
        ctx: BoundaryContext,
    ) -> BoundaryDetectionResult:
        """单步判定:对一批新消息询问 LLM 是否切边界。

        约定:``new`` 为空 → ``should_wait``;``history`` 为空 → cold_start;
        否则把 ``history + new[:1]`` 编号送入 LLM。
        """
        if not history:
            return BoundaryDetectionResult(
                should_end=False, should_wait=False, reasoning="cold_start", confidence=1.0
            )
        if not new:
            return BoundaryDetectionResult(
                should_end=False, should_wait=False, reasoning="empty_new", confidence=1.0
            )
        if self._llm_client is None:
            return BoundaryDetectionResult(
                should_end=False, should_wait=False, reasoning="llm_unbound", confidence=0.0
            )
        idx, reason, conf = await _ask_llm_boundary(
            self._llm_client,
            self._prompt_id,
            ctx.tenant_id,
            ctx.user_id,
            history + [new[0]],
        )
        # boundary_index 指向 new[0] (即 history 末端 +1);**严格相等**才切边界。
        # 旧实现 ``idx >= len(history)`` 会让 LLM 幻觉的过大 idx(如 1-indexed
        # 误用,或返回 999 hallucination)误触发切分;约束到等于这一个值。
        # 仍按"-1 / 任何 < len(history) 的值都不切",保留 fallback 不切的行为。
        should_end = idx == len(history)
        return BoundaryDetectionResult(
            should_end=should_end, should_wait=False, reasoning=reason or "llm_decision", confidence=conf
        )

    # ────────────────────────────────────────────────────────────────────────
    # 批量切分
    # ────────────────────────────────────────────────────────────────────────
    async def segment(self, raw_data_list: list[RawData]) -> list[list[RawData]]:
        """对整批输入做 LLM 切分。

        - 输入小于 2 条 → 不切
        - LLM 不可用 / 解析失败 → 整段返回
        """
        if len(raw_data_list) < 2:
            return [list(raw_data_list)] if raw_data_list else []
        # 防 prompt 过长:超过 ``max_window_turns`` 的部分留给规则路径处理
        head = raw_data_list[: self._max_window_turns]
        tail = raw_data_list[self._max_window_turns :]
        if self._llm_client is None:
            return [list(head)] + ([list(tail)] if tail else [])
        idx, reason, conf = await _ask_llm_boundary(
            self._llm_client,
            self._prompt_id,
            head[0].tenant_id,
            head[0].user_id,
            head,
        )
        logger.debug(
            "llm_sbd segment: idx=%d, reason=%s, conf=%.2f", idx, reason, conf
        )
        # 仅当 0 < idx < len(head) 才真正在 head 内做二次切分;
        # idx==0 表示头部不切,idx>=len(head) 表示"恰好在 head/tail 边界",
        # 二者都等价于"不在 head 内引入新边界",直接保留整段 head。
        if 0 < idx < len(head):
            head_split = split_segment_at(head, idx)
        else:
            head_split = [list(head)]
        if tail:
            head_split.append(list(tail))
        return [s for s in head_split if s]


# ════════════════════════════════════════════════════════════════════════════
# 共用工具
# ════════════════════════════════════════════════════════════════════════════
async def _ask_llm_boundary(
    llm_client: Any,
    prompt_id: str,
    tenant_id: str,
    user_id: str,
    segment: list[RawData],
) -> tuple[int, str, float]:
    """渲染 prompt → 调 LLM → 解析。任何环节抛异常都安全回退 ``(-1, reason, 0)``。"""
    try:
        prompt = await get_prompt_manager().render_for(
            prompt_id,
            tenant_id=tenant_id,
            user_id=user_id,
            numbered_text=format_numbered_segments(segment),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("sbd_llm_refine prompt render failed: %s", e)
        return -1, f"prompt_failed:{e.__class__.__name__}", 0.0
    try:
        resp = await llm_client.generate(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("sbd_llm_refine generate failed: %s", e)
        return -1, f"llm_failed:{e.__class__.__name__}", 0.0
    return parse_llm_boundary_response(resp)


__all__ = ["LLMSBD"]
