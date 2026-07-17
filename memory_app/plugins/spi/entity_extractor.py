"""EntityExtractor SPI —— 实体抽取（设计文档 §5.3.1）。

默认实现 ``spacy_zh_en``：spaCy NER + 多层过滤（PROPER / QUOTED / COMPOUND / NOUN）。
"""

from __future__ import annotations

from abc import abstractmethod
from enum import Enum

from pydantic import BaseModel

from memory_app.plugins.base import Plugin


class EntityType(str, Enum):
    """实体类型，按优先级降序（设计文档 §5.3.1）。"""

    PROPER = "PROPER"        # 大写首字母多词序列：人名 / 地名 / 组织名
    COMPOUND = "COMPOUND"    # 名词-名词复合短语
    QUOTED = "QUOTED"        # 引号内文本
    NOUN = "NOUN"            # 单一名词（兜底）


class Entity(BaseModel):
    """实体抽取产物。"""

    text: str               # 实体文本
    entity_type: EntityType
    normalized: str | None = None  # 大小写归一化形式（用于去重）
    confidence: float = 1.0


class EntityExtractor(Plugin):
    """实体抽取扩展点。"""

    @abstractmethod
    async def extract(self, text: str) -> list[Entity]:
        """从文本抽取实体列表。

        约定：
        - 必须做去重（按 ``normalized`` 同型只保留最高优先级类型）
        - 必须过滤泛化词（thing / stuff / way / time 等无信息量词）
        - 子串实体应被丢弃（"高速" 是 "机场高速" 子串则去掉前者）
        - 单次调用应 < 50ms（spaCy ``nlp.pipe`` 批处理）
        """

    @abstractmethod
    async def extract_batch(self, texts: list[str]) -> list[list[Entity]]:
        """批量抽取，性能优化路径。

        约定：返回值长度严格等于 ``len(texts)``；空文本返回 ``[]``。
        """


__all__ = ["EntityExtractor", "Entity", "EntityType"]
