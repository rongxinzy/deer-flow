"""Retry helpers for Feishu card updates.

Only transport failures are retried here. Business rejections (an expired card,
a revoked scope) are deterministic, so they must reach the caller and keep the
existing ``_send_with_retry`` contract intact.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import requests

logger = logging.getLogger(__name__)

# requests folds DNS/connect/TLS/timeout failures into these two families.
TRANSIENT_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


def is_transient(error: BaseException) -> bool:
    """Return True for failures a retry may plausibly fix."""
    return isinstance(error, TRANSIENT_ERRORS)


async def update_card_with_retry(
    update: Callable[[str, str], Awaitable[None]],
    message_id: str,
    text: str,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> None:
    """Patch a card, retrying transient failures with linear backoff."""
    for attempt in range(1, attempts + 1):
        try:
            await update(message_id, text)
            return
        except TRANSIENT_ERRORS:
            if attempt >= attempts:
                raise
            logger.warning(
                "[Feishu] card update failed (attempt %d/%d), retrying",
                attempt,
                attempts,
                exc_info=True,
            )
            await asyncio.sleep(base_delay * attempt)
