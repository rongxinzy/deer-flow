"""Card patches retry transport failures only, never business rejections."""

import pytest
import requests

from app.channels.feishu_card_retry import update_card_with_retry


@pytest.mark.asyncio
async def test_transient_failure_is_retried_until_it_succeeds():
    calls: list[str] = []

    async def flaky(message_id: str, text: str) -> None:
        calls.append(message_id)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError("transient TLS reset")

    await update_card_with_retry(flaky, "om_source", "hello")

    assert calls == ["om_source", "om_source", "om_source"]


@pytest.mark.asyncio
async def test_transient_failure_raises_after_max_attempts():
    calls: list[str] = []

    async def failing(message_id: str, text: str) -> None:
        calls.append(message_id)
        raise requests.exceptions.Timeout("still down")

    with pytest.raises(requests.exceptions.Timeout):
        await update_card_with_retry(failing, "om_source", "hello")

    assert len(calls) == 3, "the final attempt must re-raise"


@pytest.mark.asyncio
async def test_business_rejection_is_not_retried():
    calls: list[str] = []

    async def rejected(message_id: str, text: str) -> None:
        calls.append(message_id)
        raise RuntimeError("Feishu card update failed: code=99991400, msg=card expired")

    with pytest.raises(RuntimeError):
        await update_card_with_retry(rejected, "om_source", "hello")

    assert len(calls) == 1, "a deterministic rejection must reach the caller so send() can retry it"
