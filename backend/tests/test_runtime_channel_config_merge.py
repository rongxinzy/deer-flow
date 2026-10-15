"""Precedence tests for merge_runtime_channel_configs (pure, no event loop)."""

from __future__ import annotations

from types import SimpleNamespace

from app.channels.runtime_config_store import merge_runtime_channel_configs


def _store(data):
    return SimpleNamespace(load_all=lambda: data)


def test_runtime_config_wins_over_yaml_on_shared_keys():
    # The runtime store holds credentials entered from the UI, which exist so a
    # deployment can configure a channel "without needing a config.yaml edit".
    # When the same provider is also present in config.yaml, the UI value the
    # user just saved must win; the yaml value must not silently override it.
    channels_config = {
        "telegram": {"bot_token": "yaml_token", "webhook_secret": "from_yaml"},
    }
    connections = SimpleNamespace(enabled=True, telegram=SimpleNamespace(enabled=True))
    store = _store({"telegram": {"bot_token": "user_token", "bot_username": "mybot"}})

    merge_runtime_channel_configs(channels_config, connections, store=store)

    merged = channels_config["telegram"]
    # UI-entered value wins on the shared key
    assert merged["bot_token"] == "user_token"
    # runtime-only key is added
    assert merged["bot_username"] == "mybot"
    # yaml-only key the UI never set is preserved
    assert merged["webhook_secret"] == "from_yaml"


def test_runtime_only_provider_is_added():
    channels_config: dict = {}
    connections = SimpleNamespace(enabled=True, telegram=SimpleNamespace(enabled=True))
    store = _store({"telegram": {"bot_token": "user_token"}})

    merge_runtime_channel_configs(channels_config, connections, store=store)

    assert channels_config["telegram"] == {"bot_token": "user_token"}


def test_declared_keys_stay_owned_by_yaml():
    # config.yaml owns bot_name/group_trigger. The runtime config API persists a
    # provider's effective config, so an older copy of the name used to shadow a
    # later declarative change; the stored value must be ignored now.
    channels_config = {
        "feishu": {"enabled": True, "bot_name": "New Bot", "group_trigger": {"mention_only": True}},
    }
    connections = SimpleNamespace(enabled=True, feishu=SimpleNamespace(enabled=True))
    store = _store({"feishu": {"app_id": "cli_x", "app_secret": "s", "enabled": True, "bot_name": "Old Bot"}})

    merge_runtime_channel_configs(channels_config, connections, store=store)

    merged = channels_config["feishu"]
    assert merged["bot_name"] == "New Bot", "a stale stored name must not win"
    assert merged["group_trigger"] == {"mention_only": True}
    # Credentials still come from the store.
    assert merged["app_id"] == "cli_x"


def test_declared_key_removed_from_yaml_stays_removed():
    channels_config = {"feishu": {"enabled": True}}
    connections = SimpleNamespace(enabled=True, feishu=SimpleNamespace(enabled=True))
    store = _store({"feishu": {"app_id": "cli_x", "enabled": True, "bot_name": "Old Bot"}})

    merge_runtime_channel_configs(channels_config, connections, store=store)

    assert "bot_name" not in channels_config["feishu"], "a stored name must not revive after the CR clears it"


def test_declared_endpoint_keys_stay_owned_by_yaml():
    # domain/ws_url are rendered from the CR so a deployment can point a channel
    # at a mock or a self-hosted gateway; a copy persisted earlier must not keep
    # the old address alive after the CR changes it.
    channels_config = {
        "feishu": {"enabled": True, "domain": "http://new-mock:18086"},
        "wecom": {"enabled": True, "ws_url": "wss://new-gateway:8899"},
    }
    connections = SimpleNamespace(
        enabled=True,
        feishu=SimpleNamespace(enabled=True),
        wecom=SimpleNamespace(enabled=True),
    )
    store = _store(
        {
            "feishu": {"app_id": "cli_x", "enabled": True, "domain": "http://old-mock:18086"},
            "wecom": {"bot_id": "b", "enabled": True, "ws_url": "wss://old-gateway:8899"},
        }
    )

    merge_runtime_channel_configs(channels_config, connections, store=store)

    assert channels_config["feishu"]["domain"] == "http://new-mock:18086"
    assert channels_config["wecom"]["ws_url"] == "wss://new-gateway:8899"
    # Credentials still come from the store.
    assert channels_config["feishu"]["app_id"] == "cli_x"


def test_declared_endpoint_key_removed_from_yaml_falls_back_to_the_default():
    channels_config = {"feishu": {"enabled": True}}  # the CR cleared the endpoint
    connections = SimpleNamespace(enabled=True, feishu=SimpleNamespace(enabled=True))
    store = _store({"feishu": {"app_id": "cli_x", "enabled": True, "domain": "http://old-mock:18086"}})

    merge_runtime_channel_configs(channels_config, connections, store=store)

    assert "domain" not in channels_config["feishu"], "clearing the endpoint must fall back to the channel default"
