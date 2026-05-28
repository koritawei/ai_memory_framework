"""FileConfigCenter —— 开发态默认实现（YAML 文件后端）。

继承 :class:`BaseConfigCenter`，仅负责：

- defaults / overrides 的 YAML 解析与（可选）回写
- mtime 轮询触发 watch

所有通用流程（resolve / write / history / notify / schema 校验）由
:class:`BaseConfigCenter` 完成，本类**不重写**任何公共方法。

文件结构（与 对齐）：

```yaml
defaults:           # 五级覆盖中的 default 层（嵌套 dict）
  memory:
    generation:
      boundary_detector: { name: hybrid_sbd, params: {...} }
global_overrides:   # global 层（dotted key → entry）
  memory.generation.boundary_detector: { name: rule_sbd, params: {...} }
tenant_overrides:
  acme_corp:
    memory.retrieval.fuser: { params: { k: 80 } }
user_overrides:
  u_123:
    memory.retrieval.channels.vector: { params: { use_fisher_rao: true } }
```
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime

from memory_app._compat import utcnow
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml

from .base import ConfigChangeEvent
from ._common import BaseConfigCenter
from .resolver import ConfigResolver

logger = logging.getLogger(__name__)


def _flatten_defaults(defaults_root: dict, prefix: str = "") -> dict[str, Any]:
    """把嵌套 defaults 展开为 ``{dotted_category: entry_dict}``。

    叶子识别两种形态(均会被规范化为 plugin entry 形态
    ``{name, params, variants?}``):

    1. **plugin 叶子**:含 ``name`` 字段,直接视作 entry
    2. **prompt 简化语法糖**:含 ``template`` 字段(无 ``name``),自动转换:

       ::

           # YAML 写法( 文档形态)
           memory.prompts.episode_extraction:
             template: "..."
             variables: [text]
             variants: [...]

           # 内部展开为 plugin entry 形态
           "memory.prompts.episode_extraction": {
             "name": "episode_extraction",
             "params": {"template": "...", "variables": [text]},
             "variants": [...]
           }

    这样 default / global / tenant 各层 entry 形态一致,
    :class:`ConfigResolver._deep_merge` 能正确叠加。
    """
    out: dict[str, Any] = {}
    if not isinstance(defaults_root, dict):
        return out
    for k, v in defaults_root.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and "name" in v:
            # plugin 叶子
            out[full] = v
        elif isinstance(v, dict) and "template" in v:
            # prompt 简化语法糖 → 规范化为 entry 形态
            params = {key: val for key, val in v.items() if key != "variants"}
            entry: dict[str, Any] = {"name": k, "params": params}
            if "variants" in v:
                entry["variants"] = v["variants"]
            out[full] = entry
        elif isinstance(v, dict):
            out.update(_flatten_defaults(v, prefix=full))
    return out


class FileConfigCenter(BaseConfigCenter):
    """YAML 文件后端 ConfigCenter（开发态默认）。"""

    def __init__(
        self,
        yaml_path: str | Path,
        *,
        poll_interval: float = 1.0,
        resolver: ConfigResolver | None = None,
    ) -> None:
        super().__init__(defaults_flat={}, resolver=resolver)
        self._path = Path(yaml_path)
        self._poll_interval = poll_interval
        self._mtime: float = 0.0
        self._global_overrides: dict[str, Any] = {}
        self._tenant_overrides: dict[str, Any] = {}
        self._user_overrides: dict[str, Any] = {}
        self._history_inmem: list[dict] = []  # 简易内存历史
        self._watch_task: asyncio.Task | None = None
        # 单一 asyncio.Lock 串行化所有 overrides + mtime 修改路径:
        # - _persist_entry(async,跨 to_thread)
        # - _poll_loop → _reload_from_disk_async(async)
        # - _load_overrides(async,resolve 入口的 mtime 刷新)
        # __init__ 的 initial 加载是单线程同步上下文,绕过锁(无并发)。
        # 不再保留 threading.Lock —— 旧版双锁(threading + asyncio)互不互斥,
        # _poll_loop 与 _persist_entry 仍能交错丢更新。
        self._reload_mu_async: asyncio.Lock = asyncio.Lock()
        self._reload_from_disk_initial()

    # ════════════════════════════════════════════════════════════
    # BaseConfigCenter 4 hook 的 YAML 实现
    # ════════════════════════════════════════════════════════════
    async def _load_overrides(self) -> tuple[dict, dict, dict]:
        # mtime 轮询保险：手工修改文件后下一次 resolve 立即可见
        await self._reload_from_disk_async()
        return (
            copy.deepcopy(self._global_overrides),
            copy.deepcopy(self._tenant_overrides),
            copy.deepcopy(self._user_overrides),
        )

    async def _persist_entry(
        self,
        *,
        category: str,
        scope: str,
        scope_id: Optional[str],
        entry: dict,
        actor: str,
    ) -> int:
        # _reload_mu_async 串行化"in-memory overrides + 磁盘读写 + mtime":
        # 否则 _poll_loop 触发的 _reload_from_disk 可能在我们刚写完内存、还没刷盘时,
        # 用磁盘旧值覆盖回内存 overrides,造成 write 静默丢失。
        # 用 asyncio.Lock 而非 threading.Lock —— 此协程跨 to_thread await,
        # threading.Lock 会让其他 coroutine 调 _reload_from_disk 时阻塞事件循环。
        async with self._reload_mu_async:
            # 1. 更新内存
            if scope == "global":
                self._global_overrides[category] = entry
            elif scope == "tenant":
                assert scope_id  # base class 已校验
                self._tenant_overrides.setdefault(scope_id, {})[category] = entry
            elif scope == "user":
                assert scope_id
                self._user_overrides.setdefault(scope_id, {})[category] = entry

            # 2. 回写 YAML(异步包装到线程池,不阻塞事件循环)
            data = await self._read_raw_async()
            if scope == "global":
                data.setdefault("global_overrides", {})[category] = entry
            elif scope == "tenant":
                data.setdefault("tenant_overrides", {}).setdefault(scope_id, {})[category] = entry
            elif scope == "user":
                data.setdefault("user_overrides", {}).setdefault(scope_id, {})[category] = entry
            await self._write_raw_async(data)

            # 3. 同步 mtime（避免下次轮询误判为外部变更）
            if self._path.exists():
                self._mtime = self._path.stat().st_mtime

            # 4. 写历史（内存）
            new_version = self._version + 1
            self._history_inmem.insert(
                0,
                {
                    "category": category,
                    "scope": scope,
                    "scope_id": scope_id,
                    "name": entry["name"],
                    "params": copy.deepcopy(entry.get("params", {})),
                    "version": new_version,
                    "actor": actor,
                    "timestamp": utcnow().isoformat(),
                },
            )
            del self._history_inmem[200:]
            return new_version

    async def _read_history(self, category: str, limit: int) -> list[dict]:
        return [h for h in self._history_inmem if h["category"] == category][:limit]

    async def _spawn_watcher(
        self, on_native_event: Callable[[ConfigChangeEvent], Awaitable[None]]
    ) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._poll_loop(on_native_event))

    async def _stop_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ════════════════════════════════════════════════════════════
    # 内部辅助
    # ════════════════════════════════════════════════════════════
    def _reload_from_disk_initial(self) -> None:
        """__init__ 期同步加载(单线程上下文,无锁也安全)。"""
        if not self._path.exists():
            logger.warning("config file %s not found, starting empty", self._path)
            return
        try:
            mtime = self._path.stat().st_mtime
            with self._path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001
            logger.error("reload config %s failed: %s", self._path, e)
            return
        try:
            self.set_defaults(_flatten_defaults(data.get("defaults", {})))
            self._global_overrides = data.get("global_overrides", {}) or {}
            self._tenant_overrides = data.get("tenant_overrides", {}) or {}
            self._user_overrides = data.get("user_overrides", {}) or {}
            self._mtime = mtime
            self._version += 1
        except Exception as e:  # noqa: BLE001
            logger.error("apply initial config failed: %s", e)

    async def _reload_from_disk_async(self) -> None:
        """async 版 reload:与 _persist_entry 共用同一把 asyncio.Lock,
        避免 _poll_loop 与 write 路径交错导致丢更新。
        """
        if not self._path.exists():
            return
        try:
            # stat / open 都是磁盘 I/O,放线程池避免阻塞 loop
            mtime = await asyncio.to_thread(lambda: self._path.stat().st_mtime)
            if mtime <= self._mtime:
                return
            data = await self._read_raw_async()
        except Exception as e:  # noqa: BLE001
            logger.error("reload config %s failed: %s", self._path, e)
            return
        async with self._reload_mu_async:
            # 再次校验:进锁后可能别的 coroutine 已经 reload 过相同 mtime
            if mtime <= self._mtime:
                return
            try:
                self.set_defaults(_flatten_defaults(data.get("defaults", {})))
                self._global_overrides = data.get("global_overrides", {}) or {}
                self._tenant_overrides = data.get("tenant_overrides", {}) or {}
                self._user_overrides = data.get("user_overrides", {}) or {}
                self._mtime = mtime
                self._version += 1
                logger.info(
                    "config reloaded from %s (version=%d)",
                    self._path, self._version,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("apply reloaded config failed: %s", e)

    def _read_raw(self) -> dict:
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _write_raw(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    # ── 异步包装:把同步磁盘 I/O 推到线程池,不阻塞事件循环 ───────────────
    # _persist_entry 在 async 上下文里需要写盘;直接 open+safe_dump 会让
    # 整个 BaseConfigCenter.write 持有的 asyncio.Lock 期间事件循环被磁盘
    # I/O 卡住。慢盘 / NFS 抖动时其它 coroutine(健康检查、retrieve 等)挂起。
    async def _read_raw_async(self) -> dict:
        return await asyncio.to_thread(self._read_raw)

    async def _write_raw_async(self, data: dict) -> None:
        await asyncio.to_thread(self._write_raw, data)

    async def _poll_loop(
        self, on_event: Callable[[ConfigChangeEvent], Awaitable[None]]
    ) -> None:
        last = self._mtime
        while not self._closed:
            try:
                await asyncio.sleep(self._poll_interval)
                if not self._path.exists():
                    continue
                mtime = self._path.stat().st_mtime
                if mtime > last:
                    await self._reload_from_disk_async()
                    last = mtime
                    event = ConfigChangeEvent(
                        category="*",
                        scope="global",
                        version=self._version,
                        actor="file_watcher",
                    )
                    await on_event(event)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("config watcher error: %s", e)

    # ════════════════════════════════════════════════════════════
    # 健康检查
    # ════════════════════════════════════════════════════════════
    async def health(self) -> dict:
        if not self._path.exists():
            return {"status": "degraded", "detail": f"config file not found: {self._path}"}
        return {
            "status": "ok",
            "detail": f"version={self._version}, mtime={int(self._mtime)}",
        }


__all__ = ["FileConfigCenter"]
