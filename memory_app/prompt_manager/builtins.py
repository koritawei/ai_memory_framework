"""内置 Prompt 模板种子(设计文档 §2.8.4.1 / Step 0.7)。

═══════════════════════════════════════════════════════════════════════════════
为什么需要内置种子
═══════════════════════════════════════════════════════════════════════════════
``config/default.yaml`` 中的 ``memory.prompts`` 段由运维维护,但项目自带的
Phase 3+ 提取器(SBD/episode/semantic 等)在**最坏情况**下(配置文件丢失或
被运维误删)仍应能跑出可用结果——种子保证基础可用性。

═══════════════════════════════════════════════════════════════════════════════
Phase 0 内置 5 个 prompt_id
═══════════════════════════════════════════════════════════════════════════════

| prompt_id                          | 用途              | 消费方       |
| ---------------------------------- | ----------------- | ------------ |
| ``episode_extraction``             | 个人情景提取      | Step 3.2     |
| ``episode_extraction_group_chat``  | 群组情景提取      | Step 3.2     |
| ``semantic_extraction``            | 语义联想提取      | Step 3.3     |
| ``sbd_llm_refine``                 | SBD LLM 边界细化  | Step 3.1     |
| ``user_preference_extract``        | 用户偏好(预留)    | Phase 5+     |

═══════════════════════════════════════════════════════════════════════════════
覆盖关系
═══════════════════════════════════════════════════════════════════════════════
``ConfigCenter`` resolve 五级覆盖链已天然覆盖种子:

::

    BUILTIN_PROMPTS (本文件)
       └── default.yaml memory.prompts.* (运维维护)
                └── global_overrides
                        └── tenant_overrides
                                └── user_overrides
                                        └── variants(灰度)
"""

from __future__ import annotations

from .models import PromptSpec


# ════════════════════════════════════════════════════════════════════════════
# 默认模板内容
# ════════════════════════════════════════════════════════════════════════════
_EPISODE_EXTRACTION_TEMPLATE = """你是一个善于结构化记忆提取的助手。
从以下对话中提取情景记忆,以 JSON 数组形式返回。

每条情景记忆包含字段:
- summary: 不超过 50 字的概括
- event_time: 事件发生时间(YYYY-MM-DD,无法判定填 null)
- key_entities: 涉及的关键实体名称列表
- emotional_valence: 情绪倾向 [-1.0, 1.0]
- importance: 重要性 [0, 1]

对话内容:
{text}

仅返回 JSON,不要任何解释文字。"""


_EPISODE_EXTRACTION_GROUP_CHAT_TEMPLATE = """你是一个善于结构化记忆提取的助手。
以下是一段群组对话,从中提取情景记忆。
特别注意:每条记忆需标注主要参与者(participants 字段)。

对话内容:
{text}

参与者列表:{participants}

返回 JSON 数组,每条情景含:
- summary, event_time, key_entities, participants, emotional_valence, importance

仅返回 JSON。"""


_SEMANTIC_EXTRACTION_TEMPLATE = """从以下情景摘要与实体中归纳语义记忆(去情境化的事实/偏好/目标)。

情景摘要:
{summary}

涉及实体:
{entities}

返回 JSON 数组,每条语义记忆含:
- content: 自然语言陈述
- knowledge_type: knowledge / fact / preference / goal
- confidence: 置信度 [0, 1]
- start_time / end_time: 时间有效期(可空)

仅返回 JSON。"""


_SBD_LLM_REFINE_TEMPLATE = """以下是按时间排序的对话片段(每行带行号)。
判断从第几行开始话题切换,返回 JSON:
{{"boundary_index": <int>, "reasoning": "<简短说明>", "confidence": <0-1>}}

如果整段都属于同一话题,返回 ``{{"boundary_index": -1, ...}}``。

对话片段:
{numbered_text}"""


_SLEEP_CONSOLIDATION_TEMPLATE = """以下是一组语义相关的记忆片段:
{memories}

请从中提炼出通用的知识 / 偏好 / 习惯,每条用简洁陈述句表达。
返回 JSON 数组,每条含:
- content: 自然语言陈述
- knowledge_type: knowledge / fact / preference / goal
- confidence: 置信度 [0, 1]

仅返回 JSON。"""


_USER_PREFERENCE_EXTRACT_TEMPLATE = """从以下对话中提取用户偏好(Phase 5+ 启用)。

对话:
{text}

返回 JSON 数组,每条偏好含:
- statement: 偏好陈述
- category: travel / food / lifestyle / ...
- confidence: 置信度

仅返回 JSON。"""


# ════════════════════════════════════════════════════════════════════════════
# 公开种子表
# ════════════════════════════════════════════════════════════════════════════
BUILTIN_PROMPTS: dict[str, PromptSpec] = {
    "episode_extraction": PromptSpec(
        template=_EPISODE_EXTRACTION_TEMPLATE,
        variables=["text"],
        description="从对话提取情景记忆(默认实现)",
        version="1.0.0",
        tags=["generation", "episode"],
    ),
    "episode_extraction_group_chat": PromptSpec(
        template=_EPISODE_EXTRACTION_GROUP_CHAT_TEMPLATE,
        variables=["text", "participants"],
        description="群组对话情景提取",
        version="1.0.0",
        tags=["generation", "episode", "group_chat"],
    ),
    "semantic_extraction": PromptSpec(
        template=_SEMANTIC_EXTRACTION_TEMPLATE,
        variables=["summary", "entities"],
        description="语义记忆归纳",
        version="1.0.0",
        tags=["generation", "semantic"],
    ),
    "sbd_llm_refine": PromptSpec(
        template=_SBD_LLM_REFINE_TEMPLATE,
        variables=["numbered_text"],
        description="SBD LLM 边界细化",
        version="1.0.0",
        tags=["generation", "sbd"],
    ),
    "sleep_consolidation": PromptSpec(
        template=_SLEEP_CONSOLIDATION_TEMPLATE,
        variables=["memories"],
        description="MemScene → SemanticMemory 睡眠巩固提炼",
        version="1.0.0",
        tags=["consolidation", "sleep"],
    ),
    "user_preference_extract": PromptSpec(
        template=_USER_PREFERENCE_EXTRACT_TEMPLATE,
        variables=["text"],
        description="用户偏好提取(Phase 5+)",
        version="0.1.0",
        tags=["generation", "preference", "phase5"],
    ),
}


__all__ = ["BUILTIN_PROMPTS"]
