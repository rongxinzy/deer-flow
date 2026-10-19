"""Retrieved-document helpers for the OpenViking memory backend.

The manager renders and converts what a retriever call returned; these
functions are the pure part of that, kept out of the manager so it stays
readable.
"""

from __future__ import annotations

from typing import Any


def _extract_uris(value: Any) -> list[str]:
    """Collect ``viking://`` URIs from a glob/ls result of any nesting."""

    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.startswith("viking://") else []
    if isinstance(value, dict):
        if isinstance(value.get("uri"), str):
            return [value["uri"]]
        uris: list[str] = []
        for key in ("matches", "result", "files", "items"):
            uris.extend(_extract_uris(value.get(key)))
        return uris
    if isinstance(value, list):
        nested: list[str] = []
        for item in value:
            nested.extend(_extract_uris(item))
        return nested
    return []


def _document_uri(document: Any) -> str:
    """The OpenViking URI a retrieved document points at, if any."""

    metadata = getattr(document, "metadata", {}) or {}
    uri = metadata.get("openviking_uri") or metadata.get("source")
    return uri if isinstance(uri, str) else ""


def _document_to_fact(document: Any) -> dict[str, Any]:
    metadata = getattr(document, "metadata", {}) or {}
    uri = _document_uri(document)
    score = metadata.get("openviking_score")
    return {
        "id": uri,
        "content": str(getattr(document, "page_content", "") or ""),
        "category": metadata.get("openviking_category") or "memory",
        "confidence": score,
        "source": uri,
        "score": score,
    }


def _format_documents(documents: list[Any], *, max_chars: int) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for document in documents:
        content = " ".join(str(getattr(document, "page_content", "") or "").split())
        key = content.casefold()
        if not content or key in seen:
            continue
        seen.add(key)
        metadata = getattr(document, "metadata", {}) or {}
        category = metadata.get("openviking_category") or "memory"
        line = f"- [{category}] {content}"
        candidate = "\n".join([*lines, line])
        if len(candidate) > max_chars:
            remaining = max_chars - len("\n".join(lines)) - (1 if lines else 0)
            if remaining > 16:
                lines.append(f"{line[: max(0, remaining - 1)]}…")
            break
        lines.append(line)
    return "\n".join(lines)
