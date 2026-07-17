#!/usr/bin/env python3
"""执行《Memory 系统完整测试方案》并生成 Markdown 报告。

用法（在 memory/ 目录下）::

    uv run python test_suite/runner.py
    uv run python test_suite/runner.py --report ../docs/Memory\\ 系统测试报告.md
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
DEFAULT_REPORT = PROJECT_ROOT.parent / "docs" / "Memory 系统测试报告.md"


@dataclass
class PhaseResult:
    name: str
    description: str
    command: list[str]
    exit_code: int = -1
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    errors: int | None = None
    junit_path: Path | None = None
    failures_detail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class RunSummary:
    started_at: datetime
    phases: list[PhaseResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(p.ok for p in self.phases)


def _run_phase(
    name: str,
    description: str,
    command: list[str],
    *,
    cwd: Path,
    junit_path: Path | None = None,
) -> PhaseResult:
    import time

    t0 = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    duration = time.perf_counter() - t0
    result = PhaseResult(
        name=name,
        description=description,
        command=command,
        exit_code=proc.returncode,
        duration_s=duration,
        stdout=proc.stdout,
        stderr=proc.stderr,
        junit_path=junit_path,
    )
    if junit_path and junit_path.is_file():
        passed, failed, skipped, errors, failure_names = _parse_junit(junit_path)
        result.passed = passed
        result.failed = failed
        result.skipped = skipped
        result.errors = errors
        result.failures_detail = failure_names
    return result


def _parse_junit(path: Path) -> tuple[int, int, int, int, list[str]]:
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag == "testsuites":
        suites = root.findall("testsuite")
    else:
        suites = [root]
    passed = failed = skipped = errors = 0
    failure_names: list[str] = []
    for suite in suites:
        failures_n = int(suite.attrib.get("failures", 0))
        errors_n = int(suite.attrib.get("errors", 0))
        failed += failures_n + errors_n
        errors += errors_n
        skipped += int(suite.attrib.get("skipped", 0))
        total = int(suite.attrib.get("tests", 0))
        suite_failures = failures_n + errors_n
        passed += max(0, total - suite_failures - int(suite.attrib.get("skipped", 0)))
        for tc in suite.findall("testcase"):
            if tc.find("failure") is not None or tc.find("error") is not None:
                failure_names.append(
                    f"{tc.attrib.get('classname', '')}.{tc.attrib.get('name', '')}"
                )
    return passed, failed, skipped, errors, failure_names


def _extract_pytest_summary(stdout: str) -> str:
    for line in reversed(stdout.strip().splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            if re.search(r"\d+\s+(passed|failed|error)", line):
                return line.strip()
    return stdout.strip()[-500:] if stdout else ""


def build_phases(report_dir: Path) -> list[tuple[str, str, list[str], Path | None]]:
    py = sys.executable
    junit = lambda name: report_dir / f"junit-{name}.xml"
    return [
        (
            "gate_audit",
            "业务平面零硬依赖审计（§8.2 lint/static）",
            [py, "scripts/audit_no_hard_deps.py"],
            None,
        ),
        (
            "unit_component",
            "单元与组件测试（tests/，排除 integration）",
            [
                py,
                "-m",
                "pytest",
                "tests/",
                "-q",
                "-m",
                "not integration",
                f"--junitxml={junit('unit')}",
                "--tb=no",
            ],
            junit("unit"),
        ),
        (
            "integration",
            "集成降级测试（tests/integration/）",
            [
                py,
                "-m",
                "pytest",
                "tests/integration/",
                "-m",
                "integration",
                "-v",
                f"--junitxml={junit('integration')}",
                "--tb=short",
            ],
            junit("integration"),
        ),
        (
            "contract",
            "SPI 契约测试（tests/contract/）",
            [
                py,
                "-m",
                "pytest",
                "tests/contract/",
                "-q",
                f"--junitxml={junit('contract')}",
                "--tb=no",
            ],
            junit("contract"),
        ),
        (
            "test_suite_e2e",
            "测试方案 E2E 流程（test_suite/e2e + gates）",
            [
                py,
                "-m",
                "pytest",
                "test_suite/e2e",
                "test_suite/gates",
                "-v",
                f"--junitxml={junit('e2e')}",
                "--tb=short",
            ],
            junit("e2e"),
        ),
        (
            "test_suite_nft",
            "非功能烟测（test_suite/nft/）",
            [
                py,
                "-m",
                "pytest",
                "test_suite/nft/",
                "-m",
                "nft",
                "-v",
                f"--junitxml={junit('nft')}",
                "--tb=short",
            ],
            junit("nft"),
        ),
    ]


def render_report(summary: RunSummary, report_path: Path) -> str:
    finished = datetime.now(timezone.utc)
    lines: list[str] = [
        "# Memory 系统测试报告",
        "",
        f"> 自动生成时间（UTC）：{finished.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 执行入口：`memory/test_suite/runner.py`",
        f"> 方案依据：`docs/Memory 系统完整测试方案.md`",
        "",
        "## 1. 执行摘要",
        "",
        f"| 项目 | 值 |",
        f"| --- | --- |",
        f"| 开始时间（UTC） | {summary.started_at.strftime('%Y-%m-%d %H:%M:%S')} |",
        f"| 结束时间（UTC） | {finished.strftime('%Y-%m-%d %H:%M:%S')} |",
        f"| 阶段总数 | {len(summary.phases)} |",
        f"| 通过阶段 | {sum(1 for p in summary.phases if p.ok)} |",
        f"| 失败阶段 | {sum(1 for p in summary.phases if not p.ok)} |",
        f"| **总体结论** | **{'通过' if summary.all_ok else '未通过'}** |",
        "",
        "## 2. 分阶段结果",
        "",
        "| 阶段 | 说明 | 结果 | 耗时(s) | 用例统计 |",
        "| --- | --- | --- | ---: | --- |",
    ]

    for p in summary.phases:
        status = "通过" if p.ok else "失败"
        stats = "-"
        if p.passed is not None:
            stats = f"通过 {p.passed}"
            if p.failed:
                stats += f" / 失败 {p.failed}"
            if p.skipped:
                stats += f" / 跳过 {p.skipped}"
            if p.errors:
                stats += f" / 错误 {p.errors}"
        elif p.name == "gate_audit":
            stats = "exit 0" if p.ok else f"exit {p.exit_code}"
        lines.append(
            f"| `{p.name}` | {p.description} | {status} | {p.duration_s:.1f} | {stats} |"
        )

    lines.extend(
        [
            "",
            "## 3. 各阶段命令",
            "",
        ]
    )
    for p in summary.phases:
        cmd = " ".join(p.command)
        lines.append(f"### `{p.name}`")
        lines.append("")
        lines.append(f"```bash")
        lines.append(f"cd memory && {cmd}")
        lines.append("```")
        lines.append("")
        summary_line = _extract_pytest_summary(p.stdout)
        if summary_line:
            lines.append(f"输出摘要：`{summary_line}`")
            lines.append("")
        if p.failures_detail:
            lines.append("失败用例：")
            for name in p.failures_detail:
                lines.append(f"- `{name}`")
            lines.append("")
        if not p.ok and p.stderr:
            lines.append("<details><summary>stderr</summary>")
            lines.append("")
            lines.append("```")
            lines.append(p.stderr.strip()[:4000])
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    lines.extend(
        [
            "## 4. 测试方案覆盖映射",
            "",
            "| 方案章节 | 本报告对应阶段 |",
            "| --- | --- |",
            "| §4 单元测试 | `unit_component` |",
            "| §5 API/组件集成 | `unit_component` + `test_suite_e2e` |",
            "| §6 端到端流程 | `test_suite_e2e` |",
            "| §5.4 / §6.6 降级 | `integration` + `test_suite_e2e` |",
            "| §7 离线巩固 | `test_suite_e2e` |",
            "| §8 图与实体 | `test_suite_e2e` |",
            "| §7.1 性能烟测 | `test_suite_nft` |",
            "| SPI 契约 | `contract` |",
            "| §8.2 插件审计 | `gate_audit` |",
            "",
            "## 5. 发布准出核对（§10）",
            "",
        ]
    )

    def _ok(name: str) -> bool:
        for p in summary.phases:
            if p.name == name:
                return p.ok
        return False

    checklist = [
        ("默认 tests/ 全绿", _ok("unit_component")),
        ("integration 全绿", _ok("integration")),
        ("插件审计 exit 0", _ok("gate_audit")),
        ("test_suite E2E 全绿", _ok("test_suite_e2e")),
        ("test_suite NFT 烟测", _ok("test_suite_nft")),
    ]
    for label, ok in checklist:
        lines.append(f"- [{'x' if ok else ' '}] {label}")

    lines.extend(
        [
            "",
            "## 6. 产物路径",
            "",
            f"- JUnit XML：`memory/test_suite/reports/junit-*.xml`",
            f"- 本报告：`{report_path.relative_to(PROJECT_ROOT.parent) if report_path.is_relative_to(PROJECT_ROOT.parent) else report_path}`",
            "",
            "---",
            "",
            "*本报告由 `test_suite/runner.py` 自动生成，请勿手工编辑统计数字。*",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Memory 系统完整测试方案执行器")
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Markdown 报告输出路径",
    )
    parser.add_argument(
        "--skip-unit",
        action="store_true",
        help="跳过 tests/ 全量（仅跑 test_suite + audit，用于快速验证）",
    )
    args = parser.parse_args()

    report_dir = REPORTS_DIR
    report_dir.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    summary = RunSummary(started_at=started)

    phases_def = build_phases(report_dir)
    if args.skip_unit:
        phases_def = [p for p in phases_def if p[0] in ("gate_audit", "test_suite_e2e", "test_suite_nft")]

    for name, desc, cmd, junit in phases_def:
        print(f"\n==> [{name}] {desc}")
        result = _run_phase(name, desc, cmd, cwd=PROJECT_ROOT, junit_path=junit)
        summary.phases.append(result)
        if result.passed is not None:
            print(
                f"    exit={result.exit_code} "
                f"passed={result.passed} failed={result.failed} "
                f"skipped={result.skipped} ({result.duration_s:.1f}s)"
            )
        else:
            print(f"    exit={result.exit_code} ({result.duration_s:.1f}s)")
        if not result.ok:
            print(_extract_pytest_summary(result.stdout) or result.stderr[:300])

    report_md = render_report(summary, args.report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report_md, encoding="utf-8")
    print(f"\n报告已写入: {args.report}")

    return 0 if summary.all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
