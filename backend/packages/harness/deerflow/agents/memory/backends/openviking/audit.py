"""Access trail for the OpenViking memory backend.

Memory governance asks for a record of *who* touched memory, under which scope,
and *how much* came back (`FR-ME-04`, `FR-GV-08`). The backend used to leave
none: a recall, a capture or a management write produced nothing an operator
could look back at.

Two things stay out of every record on purpose:

* the memory content itself, which can hold personal data — a trail is not a
  second copy of the store;
* entry URIs, because the platform's log redaction collapses URL paths to
  ``/<redacted>`` (`deerflow.logging_config`), so they never reach the trail
  either. A queryable audit store could keep entry ids; the log layer only
  carries what actually survives here.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def record_access(
    mode: str,
    scope: str,
    user_id: str | None,
    agent_name: str | None,
    requester_id: str | None,
    hits: int,
    thread_id: str | None = None,
) -> None:
    """Log one memory access.

    ``mode`` names what happened (injection, search, list, capture, create,
    update, delete, clear) and ``hits`` how much of the memory was involved; a
    capture counts the messages it took in, a management call counts the
    entries it touched.
    """

    parts = [
        "mode=" + mode,
        "scope=" + scope,
        "user=" + (user_id or "-"),
        "agent=" + (agent_name or "-"),
        "requester=" + (requester_id or "-"),
        "hits=" + str(hits),
    ]
    if thread_id:
        parts.append("thread=" + thread_id)
    logger.info("memory access %s", " ".join(parts))
