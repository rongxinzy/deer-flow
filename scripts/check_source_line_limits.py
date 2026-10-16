#!/usr/bin/env python3
"""Fail when hand-written source files exceed the line budget unless ratchet-capped."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPS_PATH = REPO_ROOT / ".github" / "source-line-caps.json"
MAX_LINES = 1000

CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".cs",
        ".vue",
    }
)

SKIP_SUBSTRINGS = (
    "node_modules/",
    "pnpm-lock.yaml",
    "uv.lock",
    "package-lock.json",
    "backend/.test_durations",
    "__snapshots__/",
)

SKIP_PREFIXES = (
    "frontend/public/demo/",
    "frontend/src/components/ui/",
    "frontend/src/components/ai-elements/",
)


def _is_handwritten_source(rel: str) -> bool:
    if any(part in rel for part in SKIP_SUBSTRINGS):
        return False
    if any(rel.startswith(prefix) for prefix in SKIP_PREFIXES):
        return False
    if rel.endswith(".lock") or rel.endswith("-lock.yaml"):
        return False
    path = Path(rel)
    return path.suffix.lower() in CODE_EXTENSIONS


def _count_lines(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if not text:
        return 0
    lines = text.count("\n")
    if not text.endswith("\n"):
        lines += 1
    return lines


def _tracked_sources() -> dict[str, int]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    counts: dict[str, int] = {}
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        rel = entry.decode("utf-8")
        if not _is_handwritten_source(rel):
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            counts[rel] = _count_lines(path)
        except UnicodeDecodeError:
            continue
    return counts


def _load_caps() -> dict[str, int]:
    if not CAPS_PATH.is_file():
        return {}
    payload = json.loads(CAPS_PATH.read_text(encoding="utf-8"))
    caps = payload.get("caps")
    if not isinstance(caps, dict):
        raise ValueError(f"{CAPS_PATH}: expected object field 'caps'")
    normalized: dict[str, int] = {}
    for path, value in caps.items():
        if not isinstance(path, str) or not isinstance(value, int):
            raise ValueError(f"{CAPS_PATH}: caps entries must be path -> int")
        normalized[path] = value
    return normalized


def main() -> int:
    sources = _tracked_sources()
    caps = _load_caps()
    errors: list[str] = []

    for rel, lines in sorted(sources.items()):
        cap = caps.get(rel)
        if lines > MAX_LINES:
            if cap is None:
                errors.append(f"{rel}: {lines} lines (limit {MAX_LINES}, not in caps baseline)")
            elif lines > cap:
                errors.append(f"{rel}: {lines} lines exceeds cap {cap} (may only shrink)")
            elif cap > lines:
                errors.append(
                    f"{rel}: cap {cap} is above current {lines}; lower cap to match shrink or remove entry once under {MAX_LINES}"
                )
        elif cap is not None:
            errors.append(
                f"{rel}: {lines} lines is under {MAX_LINES}; remove stale cap entry (was {cap})"
            )

    for rel in sorted(set(caps) - set(sources)):
        errors.append(f"{rel}: cap entry present but file is not tracked source")

    if errors:
        print(f"FAIL: source line limit check ({len(errors)} issue(s)):", file=sys.stderr)
        for msg in errors:
            print(f"  {msg}", file=sys.stderr)
        print(
            f"Hand-written code must stay at or below {MAX_LINES} lines, or appear in "
            f"{CAPS_PATH.relative_to(REPO_ROOT)} with a non-increasing cap until split.",
            file=sys.stderr,
        )
        return 1

    over = sum(1 for n in sources.values() if n > MAX_LINES)
    print(
        f"PASS: {len(sources)} tracked source file(s); "
        f"{over} capped above {MAX_LINES} lines; {len(caps)} cap entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
