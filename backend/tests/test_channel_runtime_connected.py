"""Provider responses expose live transport state separately from user bindings."""

from types import SimpleNamespace

import app.channels.service as channel_service
from app.gateway.routers import channel_connections
from deerflow.config.channel_connections_config import ChannelConnectionsConfig


def _status(*, service_running: bool = True, connected: bool = True, running: bool = True):
    return {
        "service_running": service_running,
        "channels": {"feishu": {"enabled": True, "running": running, "connected": connected}},
    }


def _patch_service(monkeypatch, status):
    monkeypatch.setattr(
        channel_service,
        "get_channel_service",
        lambda: SimpleNamespace(get_status=lambda: status),
    )


def test_runtime_connected_reports_live_transport(monkeypatch):
    _patch_service(monkeypatch, _status())
    assert channel_connections._runtime_channel_connected("feishu") is True


def test_runtime_connected_is_false_when_worker_is_not_authenticated(monkeypatch):
    _patch_service(monkeypatch, _status(connected=False))
    assert channel_connections._runtime_channel_connected("feishu") is False


def test_runtime_connected_is_false_without_a_running_service(monkeypatch):
    _patch_service(monkeypatch, _status(service_running=False))
    assert channel_connections._runtime_channel_connected("feishu") is False


def test_runtime_connected_is_false_without_a_service(monkeypatch):
    monkeypatch.setattr(channel_service, "get_channel_service", lambda: None)
    assert channel_connections._runtime_channel_connected("feishu") is False


def test_provider_response_distinguishes_binding_from_transport(monkeypatch):
    _patch_service(monkeypatch, _status(connected=True))
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "feishu": {"enabled": True}})
    channels_config = {"feishu": {"enabled": True, "app_id": "synthetic", "app_secret": "synthetic"}}

    response = channel_connections._provider_response(
        config,
        channels_config,
        "feishu",
        channel_connections._PROVIDER_META["feishu"],
        None,  # no user binding yet
    )

    assert response.runtime_connected is True, "an online bot must not look offline just because nobody bound yet"
