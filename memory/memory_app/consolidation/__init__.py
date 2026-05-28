"""离线巩固管线核心。

═══════════════════════════════════════════════════════════════════════════════
模块组织
═══════════════════════════════════════════════════════════════════════════════
- :mod:`sleep`   :class:`SleepConsolidator` —— MemScene → SemanticMemory
- :mod:`decay`   :class:`DecayManager` —— 被动衰减 + 容量约束

插件层:
- ``plugins_default/three_phase_dreaming.py`` 实现 ConsolidationStrategy SPI,
  内部串联本目录下的核心算法 + Consolidator + DecayManager
- ``plugins_default/greedy_capacity_optimizer.py`` 实现 CapacityOptimizer SPI
"""

from memory_app.consolidation.decay import DecayManager
from memory_app.consolidation.sleep import SleepConsolidator

__all__ = ["SleepConsolidator", "DecayManager"]
