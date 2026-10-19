"""Channel startup must not block on remote self-identity lookups.

Regression for the live Feishu failure: ``POST /api/channels/feishu/runtime-config``
took 12.3s because ``FeishuChannel.start()`` awaited the bot-identity lookup
before the websocket thread even existed. ``SlackChannel.start()`` awaited
``auth_test`` the same way. Both lookups are now background tasks whose failure
degrades gracefully instead of delaying channel startup.

Kept in its own module: ``tests/test_channels.py`` sits exactly on its line-cap
ratchet (``.github/source-line-caps.json``), which may only shrink.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.channels.message_bus import MessageBus


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _stub_feishu_ws_thread(monkeypatch, channel) -> threading.Event:
    """Replace the lark websocket worker with a thread that lives until released."""
    gate = threading.Event()
    monkeypatch.setattr(channel, "_run_ws", lambda *args: gate.wait(10))
    return gate


def test_feishu_start_does_not_block_on_bot_identity(monkeypatch):
    """Regression: the bot-identity lookup used to be awaited inline in start().

    Live: ``POST /api/channels/feishu/runtime-config`` took 12.3s because
    ``start()`` waited out the identity client's 10s timeout before the
    websocket thread was even created — and that socket then failed to connect
    anyway. Startup must publish the channel and return.
    """
    from app.channels.feishu import FeishuChannel

    # start() imports lark-oapi lazily on its first call in a process; that
    # one-off interpreter cost is not the blocking behaviour under test, so
    # warm it outside the measured window.
    importlib.import_module("lark_oapi")

    async def go():
        channel = FeishuChannel(MessageBus(), config={"app_id": "id", "app_secret": "secret", "bot_name": "数字员工"})
        ws_gate = _stub_feishu_ws_thread(monkeypatch, channel)
        identity_gate = threading.Event()
        monkeypatch.setattr(channel, "_load_bot_identity", lambda client: identity_gate.wait(10))

        started = time.monotonic()
        await channel.start()
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"start() must not wait for the identity lookup (took {elapsed:.2f}s)"
        assert channel.is_running is True, "the channel must serve while the lookup is still pending"
        identity_task = channel._identity_task
        assert identity_task is not None and not identity_task.done(), "identity resolution must run in the background"
        assert channel._mentions_bot([SimpleNamespace(name="数字员工")]) is True, "the declared name keeps @ gating working"
        assert channel._mentions_bot([SimpleNamespace(name="同事")]) is False

        ws_gate.set()
        await channel.stop()
        assert identity_task.cancelled(), "stop() must cancel the pending identity task"
        assert channel._identity_task is None, "no unfinished identity task may be left behind"
        identity_gate.set()

    _run(go())


def test_feishu_start_survives_a_failed_identity_lookup(monkeypatch):
    """A failed lookup must not disturb the channel state or group admission."""
    from app.channels.feishu import FeishuChannel

    async def go():
        channel = FeishuChannel(MessageBus(), config={"app_id": "id", "app_secret": "secret", "bot_name": "数字员工"})
        ws_gate = _stub_feishu_ws_thread(monkeypatch, channel)

        def failing_lookup(client):
            raise RuntimeError("identity endpoint unreachable")

        monkeypatch.setattr(channel, "_load_bot_identity", failing_lookup)

        await channel.start()
        identity_task = channel._identity_task
        assert identity_task is not None
        with pytest.raises(RuntimeError):
            await identity_task

        assert channel.is_running is True and channel._running is True
        assert channel._identity_task is None, "a finished identity task must not be left behind"
        assert channel._bot_open_id is None
        mention = [SimpleNamespace(name="数字员工")]
        assert channel._should_ignore_group_message(chat_type="group", root_id=None, parent_id=None, thread_id=None, mentions=mention) is False
        assert channel._should_ignore_group_message(chat_type="group", root_id=None, parent_id=None, thread_id=None, mentions=[]) is True

        ws_gate.set()
        await channel.stop()

    _run(go())


def test_feishu_background_identity_lookup_lands_and_tightens_gating(monkeypatch):
    """The deferred lookup must still take effect for @ gating once it lands."""
    from app.channels.feishu import FeishuChannel

    async def go():
        channel = FeishuChannel(MessageBus(), config={"app_id": "id", "app_secret": "secret", "bot_name": "数字员工"})
        ws_gate = _stub_feishu_ws_thread(monkeypatch, channel)
        monkeypatch.setattr(channel, "_load_bot_identity", lambda client: setattr(channel, "_bot_open_id", "ou_bot"))

        await channel.start()
        identity_task = channel._identity_task
        assert channel._bot_open_id is None, "the lookup must not delay start()"
        await identity_task

        assert channel._bot_open_id == "ou_bot"
        assert channel._mentions_bot([{"id": {"open_id": "ou_bot"}}]) is True
        assert channel._mentions_bot([{"name": "数字员工"}]) is False, "a resolved open_id replaces the legacy name fallback"

        ws_gate.set()
        await channel.stop()

    _run(go())


def test_slack_start_does_not_block_on_bot_user_id_lookup(monkeypatch):
    """Same theme: ``auth_test`` used to be awaited inline in ``start()``."""
    from app.channels.slack import SlackChannel

    class _FakeSocketModeClient:
        def __init__(self, app_token, web_client):
            self.app_token = app_token
            self.web_client = web_client
            self.socket_mode_request_listeners: list[Any] = []

        def connect(self):  # pragma: no cover - the test stubs the connect path
            raise AssertionError("Socket Mode must not connect in this test")

        def close(self):
            pass

    monkeypatch.setattr("slack_sdk.socket_mode.SocketModeClient", _FakeSocketModeClient)

    async def go():
        gate = threading.Event()
        web_client = MagicMock()
        web_client.auth_test.side_effect = lambda: (gate.wait(10), {"user_id": "UBOT"})[1]
        channel = SlackChannel(
            MessageBus(),
            config={"bot_token": "xoxb-test", "app_token": "xapp-test", "web_client_factory": lambda token: web_client},
        )
        monkeypatch.setattr(channel, "_connect_socket_mode", lambda client: None)

        started = time.monotonic()
        await channel.start()
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"start() must not wait for auth_test (took {elapsed:.2f}s)"
        assert channel._running is True
        task = channel._bot_user_id_task
        assert task is not None and not task.done(), "the bot user id lookup must run in the background"
        assert channel._bot_user_id is None
        assert channel._web_client is web_client, "the web client itself is local and must be ready"

        await channel.stop()
        assert task.cancelled(), "stop() must cancel the pending lookup"
        assert channel._bot_user_id_task is None, "no unfinished lookup task may be left behind"
        gate.set()

    _run(go())
