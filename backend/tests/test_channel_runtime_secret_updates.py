"""Credential omission preserves existing values, never creates or clears secrets."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_channel_connections_router import _enabled_connections_config, _make_app
from test_channel_connections_router import _stub_app_config as _stub_app_config

from app.channels.runtime_config_store import ChannelRuntimeConfigStore
from app.gateway.routers import channel_connections


@pytest.mark.parametrize("provider,id_key,secret_key", [("feishu", "app_id", "app_secret"), ("wecom", "bot_id", "bot_secret"), ("wechat", None, "bot_token")])
def test_configure_provider_runtime_credentials_preserves_omitted_secrets(tmp_path, monkeypatch, provider, id_key, secret_key):
    previous = {"enabled": True, secret_key: "synthetic-old-secret"}
    submitted = {}
    if id_key:
        previous[id_key] = "synthetic-id"
        submitted[id_key] = "synthetic-id"
    store = ChannelRuntimeConfigStore(tmp_path / "runtime-config.json")
    app = _make_app(_enabled_connections_config(), None, {provider: previous}, runtime_config_store=store)
    restart = AsyncMock(return_value=True)
    monkeypatch.setattr(channel_connections, "_restart_runtime_channel_if_available", restart)
    with TestClient(app) as client:
        result = client.post(f"/api/channels/{provider}/runtime-config", json={"values": submitted})
    assert result.status_code == 200, result.text
    assert store.get_provider_config(provider)[secret_key] == "synthetic-old-secret"
    assert restart.await_args.args[1][secret_key] == "synthetic-old-secret"
    assert "synthetic-old-secret" not in result.text


@pytest.mark.parametrize("existing,submitted", [({}, {}), ({"bot_secret": "synthetic-old-secret"}, {"bot_secret": ""})])
def test_omitted_secret_cannot_create_credentials_or_clear_an_existing_secret(existing, submitted):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as rejected:
        channel_connections._required_runtime_values("wecom", {"bot_id": "synthetic-id", **submitted}, existing)
    assert rejected.value.status_code == 400


