"""``noop_fuser`` —— Phase 0/1 占位融合（继承 :class:`Fuser` SPI）。

Phase 1 起继承正式 :class:`memory_app.plugins.spi.fuser.Fuser`。
:meth:`fuse` 直接拼接所有通道结果，**不**做去重 / 加权 / RRF —— Phase 4 落地
``weighted_rrf`` 后通过配置切换。
"""

from __future__ import annotations

from typing import Any, Mapping

from memory_app.internal_models import RankedMemory
from memory_app.plugins import PluginMeta, register
from memory_app.plugins.spi.fuser import Fuser


@register
class NoopFuser(Fuser):
    """Phase 0/1 stub —— 直接拼接通道结果不做融合。"""

    meta = PluginMeta(
        name="noop_fuser",
        category="memory.retrieval.fuser",
        version="0.1.0",
        description="Phase 0/1 stub —— 直接拼接通道结果不做融合",
        # k 字段与未来 weighted_rrf 的 k=60 平滑常数同名，便于配置切换
        config_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "k": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 60},
            },
        },
    )

    def __init__(self) -> None:
        self._k: int = 60

    async def start(self, config: Mapping[str, Any]) -> None:
        self._k = int(config.get("k", 60))

    async def stop(self) -> None:
        return None

    async def fuse(
        self,
        channel_outputs: dict[str, list[RankedMemory]],
        weights: dict[str, float] | None = None,  # Phase 0/1 忽略 weights
    ) -> list[RankedMemory]:
        """直接拼接所有通道结果。生产实现应做加权 RRF（§6.1.2）。

        约定：仅做拼接，不去重；不修改入参。
        """
        merged: list[RankedMemory] = []
        for items in channel_outputs.values():
            merged.extend(items)
        return merged

    async def health(self) -> dict:
        return {"status": "ok", "detail": f"noop_fuser k={self._k}"}
