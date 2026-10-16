#!/usr/bin/env python3
"""Exit non-zero when any required CI job failed (skipped jobs are OK)."""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: ci_aggregate_gate.py '<needs-json>'", file=sys.stderr)
        return 2
    needs: dict[str, dict[str, str]] = json.loads(sys.argv[1])
    failed: list[str] = []
    for name, job in sorted(needs.items()):
        if name == "changes":
            continue
        result = job.get("result")
        if result == "failure":
            failed.append(name)
        elif result == "cancelled":
            failed.append(f"{name} (cancelled)")
    if failed:
        print("CI aggregate: failing jobs:", ", ".join(failed), file=sys.stderr)
        return 1
    print("CI aggregate: all required jobs succeeded or were skipped (path filter)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
