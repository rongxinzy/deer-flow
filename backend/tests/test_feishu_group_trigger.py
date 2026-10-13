"""Group admission exercises the real inbound callback and persisted mappings."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.channels.feishu import FeishuChannel
from app.channels.message_bus import MessageBus
from app.channels.store import ChannelStore


def _event(*, mentions=(), root_id=None, chat_type="group"):
    message = SimpleNamespace(chat_id="oc_group", message_id="om_new", chat_type=chat_type, content=json.dumps({"text": "hello"}), root_id=root_id, parent_id=None, thread_id=None, mentions=list(mentions))
    return SimpleNamespace(event=SimpleNamespace(sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_user")), message=message))


@pytest.mark.parametrize("mention", [SimpleNamespace(id=SimpleNamespace(open_id="ou_bot"), name="renamed"), {"id": {"open_id": "ou_bot"}, "name": "renamed"}])
def test_bot_open_id_survives_rename_and_dict_events(monkeypatch, mention):
    channel = FeishuChannel(MessageBus(), {"bot_open_id": "ou_bot", "bot_name": "old name"})
    dispatch = Mock()
    monkeypatch.setattr(channel, "_schedule_prepare_inbound", dispatch)
    channel._on_message(_event(mentions=[mention]))
    dispatch.assert_called_once()


def test_colleague_with_bot_display_name_is_ignored(monkeypatch):
    channel = FeishuChannel(MessageBus(), {"bot_open_id": "ou_bot", "bot_name": "same name"})
    dispatch = Mock()
    monkeypatch.setattr(channel, "_schedule_prepare_inbound", dispatch)
    channel._on_message(_event(mentions=[{"id": {"open_id": "ou_colleague"}, "name": "same name"}]))
    dispatch.assert_not_called()


def test_unknown_bot_identity_does_not_accept_arbitrary_mentions(monkeypatch):
    channel = FeishuChannel(MessageBus(), {})
    dispatch = Mock()
    monkeypatch.setattr(channel, "_schedule_prepare_inbound", dispatch)
    channel._on_message(_event(mentions=[SimpleNamespace(name="colleague")]))
    dispatch.assert_not_called()


def test_only_mapped_bot_topics_allow_unmentioned_followups(tmp_path, monkeypatch):
    store = ChannelStore(tmp_path / "channels.json")
    store.set_thread_id("feishu", "oc_group", "df_thread", topic_id="om_bot_topic", user_id="ou_user")
    channel = FeishuChannel(MessageBus(), {"channel_store": store})
    dispatch = Mock()
    monkeypatch.setattr(channel, "_schedule_prepare_inbound", dispatch)
    channel._on_message(_event(root_id="om_colleague_topic"))
    dispatch.assert_not_called()
    channel._on_message(_event(root_id="om_bot_topic"))
    dispatch.assert_called_once()


def test_bot_identity_is_discovered_through_tenant_sdk_request():
    channel = FeishuChannel(MessageBus(), {})
    client = Mock()
    client.request.return_value = SimpleNamespace(success=lambda: True, raw=SimpleNamespace(content=json.dumps({"bot": {"open_id": "ou_bot"}}).encode()))
    channel._load_bot_identity(client)
    assert channel._mentions_bot([{"id": {"open_id": "ou_bot"}}])
    request = client.request.call_args.args[0]
    assert request.uri == "/open-apis/bot/v3/info"
    assert {token.name for token in request.token_types} == {"TENANT"}


@pytest.mark.parametrize("response", [SimpleNamespace(success=lambda: False, code=999), SimpleNamespace(success=lambda: True, raw=SimpleNamespace(content=b"invalid"))])
def test_identity_lookup_failure_keeps_group_admission_closed(response):
    channel = FeishuChannel(MessageBus(), {})
    client = Mock()
    client.request.return_value = response
    channel._load_bot_identity(client)
    assert not channel._mentions_bot([{"name": "colleague"}])


@pytest.mark.parametrize("name", [" @Digital Employee ", "@digital employee"])
def test_legacy_name_fallback_supports_dict_and_whitespace(name):
    channel = FeishuChannel(MessageBus(), {"bot_name": " Digital Employee "})
    assert channel._mentions_bot([{"name": name}])
