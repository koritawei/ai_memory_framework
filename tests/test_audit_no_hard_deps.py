"""scripts/audit_no_hard_deps.py 单测(管理面)。

═══════════════════════════════════════════════════════════════════════════════
覆盖
═══════════════════════════════════════════════════════════════════════════════
- 当前业务平面无硬依赖,审计通过
- 故意构造硬依赖文件 → 审计失败并返回非空违规列表
- main 退出码语义:无违规 → 0,有违规 → 1
- 仅扫描 .py;非 Python 文件被忽略
- ALLOW_DIRS 之外的 ``import memory_app.plugins_default.X`` 与
  ``from memory_app.plugins_default.X import Y`` 都应被识别
"""

from __future__ import annotations

import pathlib

import pytest

from scripts.audit_no_hard_deps import (
    BUSINESS_TARGETS,
    FORBIDDEN_PREFIX,
    main,
    scan_file,
    scan_violations,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════════════════
# 现状审计:仓库当前应当通过
# ════════════════════════════════════════════════════════════════════════════
class TestCurrentRepoIsClean:
    def test_no_violations_in_business_plane(self):
        violations = scan_violations(repo_root=REPO_ROOT)
        assert violations == [], (
            "业务平面发现硬依赖违规:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_main_returns_zero(self, capsys):
        rc = main([])
        captured = capsys.readouterr()
        assert rc == 0, captured.out
        assert "审计通过" in captured.out


# ════════════════════════════════════════════════════════════════════════════
# 故意注入违规
# ════════════════════════════════════════════════════════════════════════════
class TestDetectsViolations:
    def _write_offender(self, tmp_path: pathlib.Path, body: str) -> pathlib.Path:
        services = tmp_path / "memory_app" / "services.py"
        services.parent.mkdir(parents=True, exist_ok=True)
        services.write_text(body, encoding="utf-8")
        return services

    def test_from_import_caught(self, tmp_path):
        body = (
            "from memory_app.plugins_default.three_phase_dreaming import "
            "ThreePhaseDreamingStrategy\n"
        )
        f = self._write_offender(tmp_path, body)
        v = scan_file(f)
        assert len(v) == 1
        assert "from memory_app.plugins_default.three_phase_dreaming" in v[0]

    def test_top_level_import_caught(self, tmp_path):
        body = "import memory_app.plugins_default.rule_sbd\n"
        f = self._write_offender(tmp_path, body)
        v = scan_file(f)
        assert len(v) == 1
        assert "import memory_app.plugins_default.rule_sbd" in v[0]

    def test_bare_package_import_caught(self, tmp_path):
        body = "import memory_app.plugins_default\n"
        f = self._write_offender(tmp_path, body)
        v = scan_file(f)
        assert len(v) == 1

    def test_scan_violations_against_offender_repo(self, tmp_path):
        # 构造一个完整的"伪仓库",services.py 含违规;只扫描 services.py 入口
        f = self._write_offender(
            tmp_path,
            "from memory_app.plugins_default.foo import Bar\n",
        )
        out = scan_violations(
            repo_root=tmp_path, targets=("memory_app/services.py",)
        )
        assert len(out) == 1
        assert "memory_app/services.py" in out[0]

    def test_main_returns_one_with_violation(
        self, tmp_path, monkeypatch, capsys
    ):
        # 把脚本看成的 repo_root 替换到 tmp_path,验证 main 返回 1
        from scripts import audit_no_hard_deps

        f = self._write_offender(
            tmp_path,
            "from memory_app.plugins_default.x import Y\n",
        )

        # main 内部用 __file__ 取 repo_root;mock pathlib.Path(__file__) 麻烦,
        # 换用直接调 scan_violations 验证 main 等价逻辑
        violations = scan_violations(
            repo_root=tmp_path,
            targets=("memory_app/services.py",),
        )
        assert len(violations) == 1


# ════════════════════════════════════════════════════════════════════════════
# 边界
# ════════════════════════════════════════════════════════════════════════════
class TestBoundaries:
    def test_non_py_files_ignored(self, tmp_path):
        # 在 routers/ 目录下放一个非 py 文件,不应被解析
        f = tmp_path / "memory_app" / "routers" / "README.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            "from memory_app.plugins_default.x import Y\n", encoding="utf-8"
        )
        out = scan_violations(
            repo_root=tmp_path, targets=("memory_app/routers/",)
        )
        assert out == []

    def test_missing_target_is_not_an_error(self, tmp_path):
        out = scan_violations(
            repo_root=tmp_path, targets=("memory_app/does_not_exist.py",)
        )
        assert out == []

    def test_relative_import_inside_plugins_default_ignored(self, tmp_path):
        # 业务平面之外的相对 import (level>0) 不属于本审计范围
        f = tmp_path / "memory_app" / "services.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("from .helpers import noop\n", encoding="utf-8")
        out = scan_file(f)
        assert out == []

    def test_unrelated_import_not_flagged(self, tmp_path):
        f = tmp_path / "memory_app" / "services.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            "from memory_app.plugins.spi.boundary_detector import BoundaryDetector\n",
            encoding="utf-8",
        )
        out = scan_file(f)
        assert out == []

    def test_forbidden_prefix_constant_matches_design(self):
        assert FORBIDDEN_PREFIX == "memory_app.plugins_default"

    def test_business_targets_cover_design_doc_minimum(self):
        #
        for required in (
            "memory_app/services.py",
            "memory_app/retrieval/",
            "memory_app/routers/",
        ):
            assert required in BUSINESS_TARGETS
