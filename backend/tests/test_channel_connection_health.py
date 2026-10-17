"""Transport workers cannot substitute for observed platform connectivity."""

import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.channels.message_bus import MessageBus
from app.channels.service import ChannelService
from app.channels.wechat import WechatChannel
from app.channels.wecom import WeComChannel


class FakeWSClient:
    def __init__(self, options):
        self.callbacks = {}
        self.is_connected = False

    def on(self, event, callback):
        self.callbacks[event] = callback

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False


@pytest.mark.asyncio
async def test_wecom_requires_authenticated_socket_and_clears_disconnected_state(monkeypatch):
    monkeypatch.setitem(sys.modules, "aibot", SimpleNamespace(WSClient=FakeWSClient, WSClientOptions=lambda **kw: kw))
    channel = WeComChannel(MessageBus(), {"bot_id": "synthetic-id", "bot_secret": "synthetic-secret"})
    await channel.start()
    try:
        await channel._ws_task
        assert channel.is_running
        assert not channel.is_connected, "socket establishment alone does not prove authentication"
        channel._ws_client.callbacks["authenticated"]()
        assert channel.is_connected
        channel._ws_client.callbacks["disconnected"]("synthetic disconnect")
        assert not channel.is_connected
        channel._ws_client.callbacks["connected"]()
        assert not channel.is_connected
        channel._ws_client.callbacks["authenticated"]()
        assert channel.is_connected
        channel._ws_client.is_connected = False
        assert not channel.is_connected, "a closed socket invalidates the last auth observation"
    finally:
        await channel.stop()
    assert not channel.is_connected


@pytest.mark.asyncio
async def test_wecom_failed_connect_task_never_reports_connected(monkeypatch):
    class FailedClient(FakeWSClient):
        async def connect(self):
            raise RuntimeError("synthetic transport failure")

    monkeypatch.setitem(sys.modules, "aibot", SimpleNamespace(WSClient=FailedClient, WSClientOptions=lambda **kw: kw))
    channel = WeComChannel(MessageBus(), {"bot_id": "synthetic-id", "bot_secret": "synthetic-secret"})
    await channel.start()
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert channel._ws_task.done()
        service = ChannelService.__new__(ChannelService)
        service._running = True
        service._config = {"wecom": {"enabled": True}}
        service._channels = {"wecom": channel}
        assert service.get_status()["channels"]["wecom"]["connected"] is False
    finally:
        await channel.stop()


@pytest.mark.parametrize("failure", ["network", "rejected", "missing-ret"])
@pytest.mark.asyncio
async def test_wechat_health_follows_successful_polls_failure_and_recovery(monkeypatch, tmp_path, failure):
    channel = WechatChannel(MessageBus(), {"bot_token": "synthetic-token", "state_dir": str(tmp_path), "polling_retry_delay": 0.001})
    entered = [asyncio.Event() for _ in range(4)]
    release = [asyncio.Event() for _ in range(4)]
    count = 0

    async def request(*args, **kwargs):
        nonlocal count
        index = count
        count += 1
        entered[index].set()
        await release[index].wait()
        if index == 1:
            if failure == "network":
                raise httpx.ConnectError("synthetic failure")
            if failure == "missing-ret":
                return {}
            return {"ret": 1, "errcode": 123}
        return {"ret": 0}

    monkeypatch.setattr(channel, "_request_json", request)
    await channel.start()
    try:
        await asyncio.wait_for(entered[0].wait(), 2)
        assert channel.is_running
        assert not channel.is_connected
        release[0].set()
        await asyncio.wait_for(entered[1].wait(), 2)
        assert channel.is_connected
        with monkeypatch.context() as clock:
            expired_at = channel._last_poll_success_at + 3600
            clock.setattr("app.channels.wechat.time", SimpleNamespace(monotonic=lambda: expired_at))
            assert not channel.is_connected, "a stuck poll cannot keep an old success alive forever"
        release[1].set()
        await asyncio.wait_for(entered[2].wait(), 2)
        assert not channel.is_connected
        release[2].set()
        await asyncio.wait_for(entered[3].wait(), 2)
        assert channel.is_connected
    finally:
        await channel.stop()
    assert not channel.is_connected
