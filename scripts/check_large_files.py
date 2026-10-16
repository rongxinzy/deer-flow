#!/usr/bin/env python3
"""Fail when tracked files exceed the size budget unless explicitly allowlisted."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = REPO_ROOT / ".github" / "large-file-allowlist.txt"
# Default budget: 1.5 MiB (binary-safe for demo assets that are intentionally larger).
MAX_BYTES = 1_572_864


def _load_allowlist() -> set[str]:
    if not ALLOWLIST.is_file():
        return set()
    entries: set[str] = set()
    for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def main() -> int:
    allowlist = _load_allowlist()
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    violations: list[tuple[int, str]] = []
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        rel = entry.decode("utf-8")
        if rel in allowlist:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_BYTES:
            violations.append((size, rel))

    if not violations:
        print(f"PASS: no tracked file exceeds {MAX_BYTES} bytes (allowlist: {len(allowlist)} paths)")
        return 0

    violations.sort(reverse=True)
    print(f"FAIL: {len(violations)} tracked file(s) exceed {MAX_BYTES} bytes:", file=sys.stderr)
    for size, rel in violations:
        mib = size / (1024 * 1024)
        print(f"  {mib:.2f} MiB  {rel}", file=sys.stderr)
    print("Add intentional assets to .github/large-file-allowlist.txt with a short comment in that file.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
