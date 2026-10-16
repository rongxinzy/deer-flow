"""Regression tests for CI helper scripts (large files, aggregate gate)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_check_large_files_passes_on_current_tree() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_large_files.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_ci_aggregate_gate_accepts_skipped_jobs() -> None:
    needs = {
        "changes": {"result": "success"},
        "large-files": {"result": "success"},
        "dead-code": {"result": "success"},
        "lint-backend": {"result": "skipped"},
        "backend-unit-tests": {"result": "success"},
    }
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci_aggregate_gate.py"), json.dumps(needs)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_ci_aggregate_gate_fails_on_failure() -> None:
    needs = {
        "changes": {"result": "success"},
        "lint-frontend": {"result": "failure"},
    }
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci_aggregate_gate.py"), json.dumps(needs)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
