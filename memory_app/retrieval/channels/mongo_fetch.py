"""检索通道共享 —— 批量拉取 MemCell。"""

from __future__ import annotations

import asyncio
from typing import Any


async def fetch_mem_cells_by_ids(
    mongo_repo: Any,
    ids: list[str],
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> list:
    """优先 ``get_by_ids``(带 scope);否则 N 次 ``get_by_id`` + 内存侧 tenant 过滤。"""
    if not ids:
        return []
    batch_fn = getattr(mongo_repo, "get_by_ids", None)
    if callable(batch_fn):
        return await batch_fn(ids, tenant_id=tenant_id, user_id=user_id)
    results = await asyncio.gather(
        *[mongo_repo.get_by_id(m) for m in ids],
        return_exceptions=False,
    )
    return [
        c
        for c in results
        if c is not None
        and (tenant_id is None or c.tenant_id == tenant_id)
        and (user_id is None or c.user_id == user_id)
    ]


__all__ = ["fetch_mem_cells_by_ids"]
