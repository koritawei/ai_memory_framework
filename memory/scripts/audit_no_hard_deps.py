"""业务平面零硬依赖审计。

═══════════════════════════════════════════════════════════════════════════════
目的
═══════════════════════════════════════════════════════════════════════════════
扫描业务平面(routers / services / retrieval / pipelines / api 等)的所有 Python
文件,确保它们**不直接 import** ``memory_app.plugins_default.*`` 下的具体实现。

业务平面应当**只**通过 :class:`PluginFactory.build` + :class:`Plugin` SPI 获取
插件实例;直接 import 默认实现会让"配置切到第三方插件"无法生效,违反"能力解锁
纯靠配置"的核心契约。

允许的硬依赖位置(在 ALLOW_DIRS 中):
- ``memory_app/api.py`` 与 ``memory_app/deps.py``  应用启动期的 plugins_default 注册触发
- ``memory_app/plugins/``                          SPI 抽象与 PluginFactory 自身
- ``memory_app/plugins_default/``                  默认插件实现自身

═══════════════════════════════════════════════════════════════════════════════
退出码
═══════════════════════════════════════════════════════════════════════════════
- ``0``  审计通过
- ``1``  发现硬依赖违规

═══════════════════════════════════════════════════════════════════════════════
集成
═══════════════════════════════════════════════════════════════════════════════
- pyproject.toml ``[project.scripts] audit-no-hard-deps = "scripts.audit_no_hard_deps:main"``
- pre-commit / CI:``uv run python scripts/audit_no_hard_deps.py``
- 也可作为单测调用 :func:`scan_violations` 直接断言
"""

from __future__ import annotations

import ast
import pathlib
import sys

#: 业务平面相对于仓库根的扫描入口(目录或文件)
BUSINESS_TARGETS: tuple[str, ...] = (
    "memory_app/services.py",
    "memory_app/feedback.py",
    "memory_app/lifecycle.py",
    "memory_app/scoring.py",
    "memory_app/clustering.py",
    "memory_app/sbd.py",
    "memory_app/consolidator.py",
    "memory_app/format_transfer.py",
    "memory_app/entity_store.py",
    "memory_app/graph_index.py",
    "memory_app/retrieval/",
    "memory_app/pipelines/",
    "memory_app/routers/",
    "memory_app/repositories/",
    "memory_app/extractors/",
    "memory_app/consolidation/",
)

#: 禁止 import 的模块前缀
FORBIDDEN_PREFIX: str = "memory_app.plugins_default"


def _matches_forbidden(module: str | None) -> bool:
    if not module:
        return False
    return module == FORBIDDEN_PREFIX or module.startswith(FORBIDDEN_PREFIX + ".")


def scan_file(path: pathlib.Path) -> list[str]:
    """扫描单个 .py 文件,返回违规字符串列表。"""
    if path.suffix != ".py":
        return []
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return [f"{path}: read failed: {e}"]
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        # 语法错误另案处理,审计不静默吞错
        return [f"{path}:{e.lineno}: parse error: {e.msg}"]

    violations: list[str] = []
    for node in ast.walk(tree):
        # ``import memory_app.plugins_default[.x]``
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_forbidden(alias.name):
                    violations.append(
                        f"{path}:{node.lineno}: import {alias.name}"
                    )
        # ``from memory_app.plugins_default[.x] import ...``
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and _matches_forbidden(module):
                imported = ", ".join(a.name for a in node.names) if node.names else "*"
                violations.append(
                    f"{path}:{node.lineno}: from {module} import {imported}"
                )
    return violations


def scan_target(repo_root: pathlib.Path, target: str) -> list[str]:
    """``target`` 可以是文件或目录(相对仓库根)。"""
    p = repo_root / target
    if not p.exists():
        return []  # 缺失文件不是错误(模块演进期允许)
    out: list[str] = []
    if p.is_dir():
        for py in sorted(p.rglob("*.py")):
            out.extend(scan_file(py))
    else:
        out.extend(scan_file(p))
    return out


def scan_violations(
    repo_root: pathlib.Path | None = None,
    targets: tuple[str, ...] | None = None,
) -> list[str]:
    """供单测 / CI 调用:返回所有违规列表。空列表表示通过。"""
    repo_root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    targets = targets or BUSINESS_TARGETS
    violations: list[str] = []
    for t in targets:
        violations.extend(scan_target(repo_root, t))
    return violations


def main(argv: list[str] | None = None) -> int:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    violations = scan_violations(repo_root=repo_root)
    if violations:
        print("插件化审计失败,业务平面存在硬依赖:")
        for v in violations:
            print(f"  - {v}")
        print(
            f"\n禁止前缀: {FORBIDDEN_PREFIX}\n"
            "请改用 PluginFactory.build(category, ...) 经 SPI 取插件实例。"
        )
        return 1
    print(
        f"插件化审计通过 ({len(BUSINESS_TARGETS)} 个业务平面入口已扫描)。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
