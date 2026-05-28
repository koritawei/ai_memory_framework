"""``three_phase_dreaming`` —— 默认 ConsolidationStrategy（离线巩固）。

═══════════════════════════════════════════════════════════════════════════════
角色
═══════════════════════════════════════════════════════════════════════════════
:class:`memory_app.plugins.spi.consolidation_strategy.ConsolidationStrategy`
的默认实现,串联三阶段:

::

    light  (近 2 天 / 弱合并 / 去重)        SleepConsolidator(threshold ↑)
    deep   (高价值深度摘要 + 衰减判定)      SleepConsolidator + DecayManager
    rem    (跨周扫描 / 主题归纳)             仅 DecayManager + capacity 优化

═══════════════════════════════════════════════════════════════════════════════
注入与失败语义
═══════════════════════════════════════════════════════════════════════════════
- ``bind_pipeline_components(...)`` 由 ``deps._init_consolidation_service`` 调,
  注入 SleepConsolidator / DecayManager / 当前 tenant_id+user_id 提供器
- 任意环节抛 → 仅记 ``ConsolidationReport.error_count``,不阻止其他阶段
- ``run(scope)`` 不持锁（当前简化实现）；生产环境可启用 Redis 分布式锁
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Mapping

from memory_app.consolidation.decay import DecayManager
from memory_app.consolidation.sleep import SleepConsolidator
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.consolidation_strategy import (
    ConsolidationReport,
    ConsolidationStrategy,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 注入辅助类型
# ════════════════════════════════════════════════════════════════════════════
ScenesProvider = Callable[[str, str], Awaitable[list]]
"""``async (tenant_id, user_id) -> list[MemScene]``;返回该 user 的成熟 scene 列表。"""


@register
class ThreePhaseDreamingStrategy(ConsolidationStrategy):
    """三相睡眠巩固策略（离线巩固默认实现）。"""

    meta = PluginMeta(
        name="three_phase",
        category="memory.lifecycle.consolidation_strategy",
        version="1.0.0",
        description="light + deep + rem 三相巩固;cron 由 ConfigCenter 控制",
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "light": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "enabled": {"type": "boolean", "default": True},
                        "cron": {"type": "string", "default": "0 */6 * * *"},
                    },
                },
                "deep": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "enabled": {"type": "boolean", "default": True},
                        "cron": {"type": "string", "default": "0 3 * * *"},
                    },
                },
                "rem": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "enabled": {"type": "boolean", "default": True},
                        "cron": {"type": "string", "default": "0 5 * * 0"},
                    },
                },
            },
        },
    )

    def __init__(self) -> None:
        self._enabled: dict[str, bool] = {"light": True, "deep": True, "rem": True}
        # 装配通过 bind_pipeline_components
        self._sleep: SleepConsolidator | None = None
        self._decay: DecayManager | None = None
        self._scenes_provider: ScenesProvider | None = None
        self._scope_provider: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None

    # ────────────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────────────
    async def start(self, config: Mapping[str, Any]) -> None:
        for phase in ("light", "deep", "rem"):
            entry = config.get(phase) or {}
            self._enabled[phase] = bool(entry.get("enabled", True))
        logger.info(
            "three_phase_dreaming started: light=%s, deep=%s, rem=%s",
            *[self._enabled[p] for p in ("light", "deep", "rem")],
        )

    async def stop(self) -> None:
        return None

    async def health(self) -> dict:
        return {
            "status": "ok",
            "detail": (
                f"light={self._enabled['light']}, "
                f"deep={self._enabled['deep']}, "
                f"rem={self._enabled['rem']}, "
                f"sleep={'bound' if self._sleep else 'unbound'}, "
                f"decay={'bound' if self._decay else 'unbound'}"
            ),
        }

    # ────────────────────────────────────────────────────────────────────────
    # 装配(由 deps 注入业务对象)
    # ────────────────────────────────────────────────────────────────────────
    def bind_pipeline_components(
        self,
        *,
        sleep: SleepConsolidator | None,
        decay: DecayManager | None,
        scenes_provider: ScenesProvider | None = None,
        scope_provider: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None,
    ) -> None:
        """注入 SleepConsolidator / DecayManager 等执行单元。

        :param scenes_provider:  ``async(tenant_id, user_id) -> list[MemScene]``,
                                 返回该用户当前的成熟 MemScene
        :param scope_provider:   ``async -> list[(tenant_id, user_id)]``,
                                 返回本次巩固扫描的所有 (tenant, user) 对
        """
        self._sleep = sleep
        self._decay = decay
        self._scenes_provider = scenes_provider
        self._scope_provider = scope_provider

    def set_scope_provider(
        self,
        scope_provider: Callable[[], Awaitable[list[tuple[str, str]]]] | None,
    ) -> None:
        """单独替换 ``scope_provider``,不影响已绑定的 sleep / decay / scenes_provider。

        供 :meth:`ConsolidationService.consolidate` 在每次调用前按入参的
        ``(tenant_id, user_id)`` 临时缩窗使用,避免直接 mutate private 字段。
        """
        self._scope_provider = scope_provider

    # ────────────────────────────────────────────────────────────────────────
    # SPI
    # ────────────────────────────────────────────────────────────────────────
    async def run(
        self,
        scope: Literal["light", "deep", "rem", "all"] = "all",
        time: datetime | None = None,
    ) -> ConsolidationReport:
        phase = self._select_phase(scope, time)
        started = _utcnow()
        report = ConsolidationReport(
            phase=phase,
            started_at=started,
            finished_at=started,
        )
        if not self._enabled.get(phase, False):
            report.notes.append(f"phase_{phase}_disabled_by_config")
            report.finished_at = _utcnow()
            return report

        # 1. 拉取本次扫描的 (tenant, user) 列表
        scopes: list[tuple[str, str]] = []
        if self._scope_provider is not None:
            try:
                scopes = list(await self._scope_provider())
            except Exception as e:  # noqa: BLE001
                report.notes.append(f"scope_provider_failed:{e.__class__.__name__}")
                report.error_count += 1

        # 2. light / deep:跑 SleepConsolidator
        if phase in ("light", "deep") and self._sleep is not None:
            for tenant_id, user_id in scopes:
                scenes = await self._fetch_scenes(tenant_id, user_id, report)
                report.scanned_count += len(scenes)
                if not scenes:
                    continue
                try:
                    items = await self._sleep.consolidate_scenes(scenes)
                    report.consolidated_count += len(items)
                except Exception as e:  # noqa: BLE001
                    report.notes.append(
                        f"sleep_consolidate_failed:{tenant_id}:{user_id}:{e.__class__.__name__}"
                    )
                    report.error_count += 1

        # 3. deep / rem:跑 DecayManager(被动衰减 + 容量约束)
        if phase in ("deep", "rem") and self._decay is not None:
            for tenant_id, user_id in scopes:
                try:
                    decay_out = await self._decay.run_passive_decay(tenant_id, user_id)
                    report.archived_count += int(decay_out.get("archived_count", 0))
                except Exception as e:  # noqa: BLE001
                    report.notes.append(
                        f"decay_failed:{tenant_id}:{user_id}:{e.__class__.__name__}"
                    )
                    report.error_count += 1
                try:
                    cap_out = await self._decay.enforce_capacity(tenant_id, user_id)
                    report.archived_count += int(cap_out.get("archived_count", 0))
                except Exception as e:  # noqa: BLE001
                    report.notes.append(
                        f"capacity_failed:{tenant_id}:{user_id}:{e.__class__.__name__}"
                    )
                    report.error_count += 1

        report.finished_at = _utcnow()
        return report

    # ────────────────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _select_phase(
        scope: Literal["light", "deep", "rem", "all"],
        time: datetime | None,
    ) -> Literal["light", "deep", "rem"]:
        """``scope=all`` 时按当前时间(本地)粗略选择:周日 → rem,03~05 → deep,
        其余 → light。简化策略,精确 cron 由调度器决定。"""
        if scope in ("light", "deep", "rem"):
            return scope  # type: ignore[return-value]
        t = time or _utcnow()
        # 周日 = weekday == 6
        if t.weekday() == 6 and 5 <= t.hour < 7:
            return "rem"
        if 3 <= t.hour < 5:
            return "deep"
        return "light"

    async def _fetch_scenes(
        self, tenant_id: str, user_id: str, report: ConsolidationReport
    ) -> list:
        if self._scenes_provider is None:
            return []
        try:
            return list(await self._scenes_provider(tenant_id, user_id))
        except Exception as e:  # noqa: BLE001
            report.notes.append(
                f"scenes_provider_failed:{tenant_id}:{user_id}:{e.__class__.__name__}"
            )
            report.error_count += 1
            return []


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["ThreePhaseDreamingStrategy", "ScenesProvider"]
