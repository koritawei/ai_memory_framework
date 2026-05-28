"""插件化审计门禁（测试方案  lint/static）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.gate
def test_audit_no_hard_deps_exit_zero():
    script = PROJECT_ROOT / "scripts" / "audit_no_hard_deps.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
