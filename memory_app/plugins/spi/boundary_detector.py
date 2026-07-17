"""BoundaryDetector SPI —— 语义边界检测 SBD（设计文档 §5.1.3）。

实现职责：在 RawData 滑动窗口上判定是否切分新的 MemCell。
默认实现 ``hybrid_sbd``（Phase 2 落地）= 规则优先 + LLM 兜底，
对应 §12.2 优化建议中的「规则模式 P95 < 10ms / LLM 模式 P95 < 2s」目标。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel, ConfigDict

from memory_app.internal_models import RawData
from memory_app.plugins.base import Plugin


class BoundaryContext(BaseModel):
    """SBD 调用上下文。"""

    model_config = ConfigDict(extra="allow")

    tenant_id: str
    user_id: str
    group_id: str | None = None  # 群聊场景
    current_time: str  # ISO8601


class BoundaryDetectionResult(BaseModel):
    """SBD 判定结果（设计文档 §5.1.3.3）。"""

    model_config = ConfigDict(extra="allow")

    should_end: bool                # True = 切分
    should_wait: bool               # True = 等待更多消息
    reasoning: str = ""             # 判定理由（便于审计 / 排查）
    confidence: float = 1.0         # 置信度 [0, 1]
    topic_summary: str | None = None  # 主题摘要（LLM 模式生成）


class BoundaryDetector(Plugin):
    """SBD（§5.1.3）扩展点。规则与 LLM 实现都继承本类。"""

    @abstractmethod
    async def detect(
        self,
        history: list[RawData],
        new: list[RawData],
        ctx: BoundaryContext,
    ) -> BoundaryDetectionResult:
        """判定新一批消息是否需要切分新 MemCell。

        约定：
        - ``history`` 为空（首条消息）→ ``should_end=False, should_wait=False``，
          ``reasoning="cold_start"``
        - 时间间隔 > 阈值 / 窗口满 / 显式分隔指令 → ``should_end=True``
        - 主题相似但置信度低 → ``should_wait=True`` 等待更多消息
        - 实现内部异常应包装为 :class:`PluginError(category="internal", retryable=True)`
        - 调用方负责 timeout 控制；超时由框架转 ``PluginTimeout``
        """


__all__ = ["BoundaryDetector", "BoundaryContext", "BoundaryDetectionResult"]
