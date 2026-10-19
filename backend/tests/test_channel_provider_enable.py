"""Router tests for toggling one channel provider without restarting the runtime.

``PUT /api/channels/{provider}/enabled`` is what the control service calls so a
channel enable or disable takes effect on the live runtime instead of rolling the
whole employee Deployment. These cases live apart from
``test_channel_connections_router.py`` because that module sits at its line cap.
"""

from __future__ import annotations

from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from _router_auth_helpers import make_authed_test_app
from fastapi.testclient import TestClient

from app.channels.runtime_config_store import ChannelRuntimeConfigStore
from app.gateway.auth.models import User
from app.gateway.routers import channel_connections
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.config.channel_connections_config import ChannelConnectionsConfig


@pytest.fixture(autouse=True)
def _stub_app_config(monkeypatch):
    """Keep these router tests independent from a developer-local config.yaml."""
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "0")
    monkeypatch.delenv("GATEWAY_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    yield
    reset_app_config()


def _user() -> User:
    return User(
        id=UUID("11111111-2222-3333-4444-555555555555"),
        email="alice@example.com",
        password_hash="x",
        system_role="admin",
    )


def _non_admin_user() -> User:
    return User(
        id=UUID("99999999-8888-7777-6666-555555555555"),
        email="bob@example.com",
        password_hash="x",
        system_role="user",
    )


async def _make_repo(tmp_path):
    from deerflow.persistence.channel_connections import ChannelConnectionRepository
    from deerflow.persistence.engine import get_session_factory, init_engine

    await init_engine("sqlite", url=f"sqlite+aiosqlite:///{tmp_path / 'provider-enable.db'}", sqlite_dir=str(tmp_path))
    return ChannelConnectionRepository(get_session_factory())


def _make_app(config: ChannelConnectionsConfig, repo, channels_config: dict | None = None):
    app = make_authed_test_app(user_factory=_user)
    app.state.channel_connections_config = config
    app.state.channel_connection_repo = repo
    app.state.channels_config = channels_config or {}
    runtime_config_dir = TemporaryDirectory()
    app.state.channel_runtime_config_tmpdir = runtime_config_dir
    app.state.channel_runtime_config_store = ChannelRuntimeConfigStore(f"{runtime_config_dir.name}/runtime-config.json")
    app.include_router(channel_connections.router)
    return app


def test_set_channel_provider_enabled_requires_admin(tmp_path):
    import anyio

    repo = anyio.run(_make_repo, tmp_path)
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "feishu": {"enabled": True}})
    app = make_authed_test_app(user_factory=_non_admin_user)
    app.state.channel_connections_config = config
    app.state.channel_connection_repo = repo
    app.state.channels_config = {}
    runtime_config_dir = TemporaryDirectory()
    app.state.channel_runtime_config_tmpdir = runtime_config_dir
    app.state.channel_runtime_config_store = ChannelRuntimeConfigStore(f"{runtime_config_dir.name}/runtime-config.json")
    app.include_router(channel_connections.router)

    with TestClient(app) as client:
        response = client.put("/api/channels/feishu/enabled", json={"enabled": True})

    assert response.status_code == 403
    assert "Admin privileges" in response.json()["detail"]

    anyio.run(repo.close)


def test_set_channel_provider_enabled_rejects_unknown_provider(tmp_path):
    import anyio

    repo = anyio.run(_make_repo, tmp_path)
    app = _make_app(ChannelConnectionsConfig.model_validate({"enabled": True, "feishu": {"enabled": True}}), repo)

    with TestClient(app) as client:
        response = client.put("/api/channels/nope/enabled", json={"enabled": True})

    assert response.status_code == 404

    anyio.run(repo.close)


def test_enable_provider_flags_the_store_and_starts_a_configured_channel(tmp_path, monkeypatch):
    import anyio

    repo = anyio.run(_make_repo, tmp_path)
    # The declarative flag is still false — that is the file a hot enable runs
    # against until the employee restarts.
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "feishu": {"enabled": False}})
    channels_config = {"feishu": {"enabled": True, "app_id": "feishu-app", "app_secret": "feishu-secret"}}
    app = _make_app(config, repo, channels_config)
    status = {"service_running": True, "channels": {"feishu": {"enabled": True, "running": False}}}
    reconciled: list[tuple[str, dict]] = []

    async def ensure_channel_ready(provider, runtime_config):
        reconciled.append((provider, dict(runtime_config)))
        status["channels"][provider]["running"] = True
        return True

    monkeypatch.setattr(
        "app.channels.service.get_channel_service",
        lambda: SimpleNamespace(get_status=lambda: status, ensure_channel_ready=ensure_channel_ready),
    )

    with TestClient(app) as client:
        response = client.put("/api/channels/feishu/enabled", json={"enabled": True})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    # Credentials come from the effective runtime config (the store plus the
    # config.yaml the operator renders), not from the declarative flag: the plain
    # field proves they are live, and the password field stays masked.
    assert body["credential_values"]["app_id"] == "feishu-app"
    assert body["credential_values"]["app_secret"]
    assert [name for name, _ in reconciled] == ["feishu"]
    # The leg starts from the effective config, credentials included.
    assert reconciled[0][1]["app_secret"] == "feishu-secret"
    assert app.state.channel_runtime_config_store.get_provider_config("feishu") == {"enabled": True}
    assert app.state.channels_config["feishu"]["app_secret"] == "feishu-secret"
    # The running process follows the live state; the booted file stays a
    # bootstrap copy.
    assert config.feishu.enabled is True

    anyio.run(repo.close)


def test_enable_provider_without_credentials_flags_the_store_only(tmp_path, monkeypatch):
    import anyio

    repo = anyio.run(_make_repo, tmp_path)
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "wecom": {"enabled": False}})
    app = _make_app(config, repo)
    ensure = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.channels.service.get_channel_service",
        lambda: SimpleNamespace(get_status=lambda: {"service_running": True, "channels": {}}, ensure_channel_ready=ensure),
    )

    with TestClient(app) as client:
        response = client.put("/api/channels/wecom/enabled", json={"enabled": True})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True and body["configured"] is False
    ensure.assert_not_awaited()
    assert app.state.channel_runtime_config_store.get_provider_config("wecom") == {"enabled": True}

    anyio.run(repo.close)


def test_disable_provider_marks_the_store_disconnected_and_clears_live_state(tmp_path):
    import anyio

    repo = anyio.run(_make_repo, tmp_path)
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "feishu": {"enabled": True}})
    channels_config = {"feishu": {"enabled": True, "app_id": "feishu-app", "app_secret": "feishu-secret"}}
    app = _make_app(config, repo, channels_config)

    with TestClient(app) as client:
        response = client.put("/api/channels/feishu/enabled", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    stored = app.state.channel_runtime_config_store.get_provider_config("feishu")
    assert stored is not None and stored["enabled"] is False
    assert "feishu" not in app.state.channels_config
    assert config.feishu.enabled is False

    anyio.run(repo.close)


def test_disable_provider_succeeds_when_the_bootstrap_flag_is_stale(tmp_path):
    """A process started before the toggle still reads "disabled" in its file.

    The control service's explicit disable must not depend on that stale copy:
    there is nothing left to stop, so the request is already satisfied.
    """
    import anyio

    repo = anyio.run(_make_repo, tmp_path)
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "feishu": {"enabled": False}})
    app = _make_app(config, repo)

    with TestClient(app) as client:
        response = client.put("/api/channels/feishu/enabled", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    stored = app.state.channel_runtime_config_store.get_provider_config("feishu")
    assert stored is not None and stored["enabled"] is False

    anyio.run(repo.close)


def test_storing_credentials_succeeds_when_the_bootstrap_flag_is_stale(tmp_path):
    """Saving credentials is itself the intent to use the provider."""
    import anyio

    repo = anyio.run(_make_repo, tmp_path)
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "wecom": {"enabled": False}})
    app = _make_app(config, repo)

    with TestClient(app) as client:
        response = client.post(
            "/api/channels/wecom/runtime-config",
            json={"values": {"bot_id": "wecom-bot", "bot_secret": "wecom-secret"}},
        )

    assert response.status_code == 200
    stored = app.state.channel_runtime_config_store.get_provider_config("wecom")
    assert stored is not None and stored["bot_id"] == "wecom-bot"
    assert app.state.channels_config["wecom"]["bot_secret"] == "wecom-secret"

    anyio.run(repo.close)
