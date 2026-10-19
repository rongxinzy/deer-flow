"""Card patches retry transient transport failures before falling back to a new reply."""

import pytest

from app.channels.feishu import FeishuChannel
from app.channels.message_bus import MessageBus


def _channel() -> FeishuChannel:
    return FeishuChannel(MessageBus(), {})


@pytest.mark.asyncio
async def test_card_update_retries_until_it_succeeds():
    channel = _channel()
    calls: list[str] = []

    async def flaky(message_id: str, text: str) -> None:
        calls.append(message_id)
        if len(calls) < 3:
            raise RuntimeError("transient TLS reset")

    channel._update_card = flaky  # type: ignore[method-assign]

    await channel._update_card_with_retry("om_source", "hello")

    assert calls == ["om_source", "om_source", "om_source"]


@pytest.mark.asyncio
async def test_card_update_raises_after_max_retries():
    channel = _channel()
    calls: list[str] = []

    async def failing(message_id: str, text: str) -> None:
        calls.append(message_id)
        raise RuntimeError("permanent rejection")

    channel._update_card = failing  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await channel._update_card_with_retry("om_source", "hello")

    assert len(calls) == 3, "the final attempt must re-raise instead of looping"
