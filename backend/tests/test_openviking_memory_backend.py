"""Tests for the official-package OpenViking memory backend."""

from __future__ import annotations

import copy
import gc
import json
import socket
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.memory.backends.openviking.config import OpenVikingConfig
from deerflow.agents.memory.backends.openviking.openviking_manager import (
    OpenVikingMemoryManager,
    RequesterScopeError,
    _canonical_peer_id,
    _session_id,
)
from deerflow.agents.memory.backends.openviking.session import _requester_peer_id
from deerflow.agents.memory.manager import (
    MemoryManagerError,
    MemoryReadError,
    _scan_backends,
    reset_memory_manager,
)
from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware


class _CommitPolicy:
    def __init__(self, *, mode: str, pending_token_threshold: int = 8_000):
        self.mode = mode
        self.pending_token_threshold = pending_token_threshold


class _PartialWriteError(RuntimeError):
    def __init__(
        self,
        consumed: int,
        *,
        commit_pending: bool = False,
    ) -> None:
        super().__init__("partial")
        self.input_messages_consumed = consumed
        self.commit_pending = commit_pending


class _Client:
    supports_request_actor_peer = True

    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        self.closed = False
        self.healthy = True
        self.files: dict[str, str] = {}
        self.removals: list[tuple[str, bool]] = []
        self.reads: list[str] = []
        self.ls_calls: list[str] = []
        self.glob_error: Exception | None = None

    def glob(self, *, pattern: str, uri: str) -> list[str]:
        if self.glob_error is not None:
            raise self.glob_error
        prefix = uri.rstrip("/") + "/"
        return [path for path in self.files if path.startswith(prefix)]

    def read(self, *, uri: str) -> str:
        self.reads.append(uri)
        return self.files[uri]

    def ls(self, *, uri: str, simple: bool = False) -> list[str]:
        self.ls_calls.append(uri)
        if self.glob_error is not None:
            raise self.glob_error
        prefix = uri.rstrip("/") + "/"
        return sorted({prefix + path[len(prefix) :].split("/", 1)[0] for path in self.files if path.startswith(prefix)})

    def rm(self, *, uri: str, recursive: bool = False, **_kwargs: Any) -> None:
        self.removals.append((uri, recursive))

    def health(self) -> bool:
        return self.healthy

    def close(self) -> None:
        self.closed = True


class _Recorder:
    def __init__(
        self,
        *,
        commit_policy: _CommitPolicy,
        url: str,
        api_key: str,
        timeout: float,
        extra_headers: dict[str, str],
    ) -> None:
        self.commit_policy = commit_policy
        self.connection = {
            "url": url,
            "api_key": api_key,
            "timeout": timeout,
            "extra_headers": extra_headers,
        }
        self._client = _Client(**self.connection)
        self.calls: list[tuple[str, list[Any], str | None, str | None]] = []
        self.flushes: list[tuple[str, str | None]] = []
        self.failures: list[BaseException] = []
        self.closed = False

    @property
    def client(self) -> _Client:
        return self._client

    def record(
        self,
        session_id: str,
        messages: list[Any],
        peer_id: str | None = None,
    ) -> object:
        self.calls.append((session_id, list(messages), peer_id, _ACTOR_PEER.get()))
        if self.failures:
            raise self.failures.pop(0)
        return object()

    def flush(self, session_id: str) -> None:
        self.flushes.append((session_id, _ACTOR_PEER.get()))
        if self.failures:
            raise self.failures.pop(0)

    def close(self) -> None:
        self.closed = True
        self._client.close()


class _Retriever:
    def __init__(self, *, client: _Client, **kwargs: Any):
        self.client = client
        self.kwargs = kwargs
        self.limit = kwargs["limit"]
        self.filter = None
        self.session_id = kwargs.get("session_id")
        self.target_uri = kwargs.get("target_uri", "")
        self.calls: list[dict[str, Any]] = []

    def __copy__(self) -> _Retriever:
        copied = type(self)(client=self.client, **self.kwargs)
        copied.calls = self.calls
        copied.limit = self.limit
        copied.filter = copy.deepcopy(self.filter)
        copied.session_id = self.session_id
        copied.target_uri = copy.deepcopy(self.target_uri)
        return copied

    def invoke(self, query: str) -> list[Document]:
        self.calls.append(
            {
                "query": query,
                "actor_peer": _ACTOR_PEER.get(),
                "limit": self.limit,
                "filter": copy.deepcopy(self.filter),
                "session_id": self.session_id,
                "target_uri": copy.deepcopy(self.target_uri),
                "search_mode": getattr(self, "search_mode", "find"),
            }
        )
        return [
            Document(
                page_content="Prefers concise answers.",
                metadata={
                    "openviking_uri": ("viking://user/memories/preferences/style.md"),
                    "openviking_category": "preferences",
                    "openviking_score": 0.91,
                },
            )
        ]


_ACTOR_PEER: ContextVar[str | None] = ContextVar(
    "test_actor_peer",
    default=None,
)


@contextmanager
def _use_actor_peer(peer_id: str | None):
    token = _ACTOR_PEER.set(peer_id)
    try:
        yield
    finally:
        _ACTOR_PEER.reset(token)


@pytest.fixture
def official_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    import deerflow.agents.memory.backends.openviking.openviking_manager as module

    monkeypatch.setattr(
        module,
        "_load_official_integration",
        lambda: {
            "OpenVikingCommitPolicy": _CommitPolicy,
            "OpenVikingPartialWriteError": _PartialWriteError,
            "OpenVikingRetriever": _Retriever,
            "OpenVikingSessionRecorder": _Recorder,
            "use_actor_peer": _use_actor_peer,
            "call_openviking": lambda client, method, /, **kwargs: getattr(client, method)(**kwargs),
        },
    )


def _backend_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "base_url": "http://openviking:1933",
        "storage_path": str(tmp_path),
        "owner_user_id": "alice",
        "startup_policy": "warn",
        "retrieval": {
            "top_k": 4,
            "max_injection_chars": 1_000,
            "injection_query": "profile preferences and prior decisions",
        },
    }
    config.update(overrides)
    return config


def _manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **overrides: Any,
) -> OpenVikingMemoryManager:
    monkeypatch.setenv("OPENVIKING_API_KEY", "user-key")
    return OpenVikingMemoryManager.from_config(_backend_config(tmp_path, **overrides))


@pytest.fixture
def unreachable_openviking_url() -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved_socket:
        reserved_socket.bind(("127.0.0.1", 0))
        host, port = reserved_socket.getsockname()
        yield f"http://{host}:{port}"


def test_config_uses_single_user_key_and_rejects_legacy_trusted_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENVIKING_API_KEY", raising=False)
    with pytest.raises(ValueError, match="USER API key"):
        OpenVikingConfig.from_backend_config(_backend_config(tmp_path))

    monkeypatch.setenv("OPENVIKING_API_KEY", "secret")
    config = OpenVikingConfig.from_backend_config(_backend_config(tmp_path))

    assert config.owner_user_id == "alice"
    assert config.content_mode == "overview"
    assert config.injection_query == "profile preferences and prior decisions"
    assert "secret" not in repr(config)

    with pytest.raises(ValueError, match="trusted mode is no longer supported"):
        OpenVikingConfig.from_backend_config(_backend_config(tmp_path, auth_mode="trusted"))
    with pytest.raises(ValueError, match="Unknown OpenViking"):
        OpenVikingConfig.from_backend_config(_backend_config(tmp_path, typo=True))
    with pytest.raises(ValueError, match="reserved prefix"):
        OpenVikingConfig.from_backend_config(_backend_config(tmp_path, default_peer_id="df-agent-default"))
    with pytest.raises(ValueError, match="custom HTTP client fields"):
        OpenVikingConfig.from_backend_config(_backend_config(tmp_path, max_connections=10))


@pytest.mark.parametrize(
    ("section", "key", "attr", "default"),
    [
        (None, "timeout_seconds", "timeout_seconds", 30.0),
        (None, "max_seen_message_ids", "max_seen_message_ids", 512),
        ("retrieval", "top_k", "search_top_k", 8),
        ("retrieval", "max_injection_chars", "max_injection_chars", 12_000),
        ("retrieval", "score_threshold", "score_threshold", None),
    ],
)
@pytest.mark.parametrize("unset", [None, "", "   "])
def test_numeric_knob_written_without_a_value_keeps_its_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str | None,
    key: str,
    attr: str,
    default: Any,
    unset: Any,
) -> None:
    """An unquoted ``top_k:`` in YAML parses as unset, not as a broken backend."""
    monkeypatch.setenv("OPENVIKING_API_KEY", "user-key")
    config = _backend_config(tmp_path)
    target = config if section is None else config[section]
    target[key] = unset

    cfg = OpenVikingConfig.from_backend_config(config)

    assert getattr(cfg, attr) == default


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        (None, "timeout_seconds", "soon"),
        (None, "timeout_seconds", [30]),
        (None, "max_seen_message_ids", "many"),
        ("retrieval", "top_k", "four"),
        ("retrieval", "top_k", {"count": 4}),
        ("retrieval", "score_threshold", "high"),
    ],
)
def test_non_numeric_knob_names_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str | None,
    key: str,
    value: Any,
) -> None:
    """The report has to say which knob is wrong, not just that a cast failed."""
    monkeypatch.setenv("OPENVIKING_API_KEY", "user-key")
    config = _backend_config(tmp_path)
    target = config if section is None else config[section]
    target[key] = value

    with pytest.raises(ValueError, match=f"OpenViking {key} must be a number"):
        OpenVikingConfig.from_backend_config(config)


def test_backend_is_discovered_by_registered_name() -> None:
    reset_memory_manager()
    assert _scan_backends()["openviking"] is OpenVikingMemoryManager


@pytest.mark.parametrize(
    ("read_policy", "expected"),
    [
        pytest.param("fail_open", False, id="fail_open"),
        pytest.param("raise", True, id="raise"),
    ],
)
def test_read_failure_capability_matches_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_policy: str,
    expected: bool,
) -> None:
    manager = _manager(
        tmp_path,
        monkeypatch,
        failure_policy={"read": read_policy},
    )

    assert manager.read_failures_are_fatal is expected


def test_official_loader_uses_standalone_package() -> None:
    from deerflow.agents.memory.backends.openviking.openviking_manager import (
        _load_official_integration,
    )

    integration = _load_official_integration()

    assert integration["OpenVikingSessionRecorder"].__module__.startswith("langchain_openviking")
    assert integration["OpenVikingRetriever"].__module__.startswith("langchain_openviking")


def test_manager_uses_official_recorder_retriever_and_commit_always(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    assert manager._recorder.commit_policy.mode == "always"
    assert manager._retriever.client is manager._recorder.client
    assert manager._retriever.kwargs["content_mode"] == "overview"
    assert manager._recorder.connection == {
        "url": "http://openviking:1933",
        "api_key": "user-key",
        "timeout": 30.0,
        "extra_headers": {},
    }


def test_context_preserves_existing_fixed_query_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    context = manager.get_context(
        "alice",
        agent_name="research",
        thread_id="thread-1",
    )

    assert context == "- [preferences] Prefers concise answers."
    assert manager._retriever.calls == [
        {
            "query": "profile preferences and prior decisions",
            "actor_peer": "research",
            "limit": 4,
            "filter": None,
            "session_id": _session_id("alice", "research", "thread-1"),
            # OpenViking >= 0.4 rejects the uid-less "viking://user/..."
            # spelling at the request boundary (session.py).
            "target_uri": [
                "viking://~/memories",
                "viking://~/peers/research/memories",
            ],
            "search_mode": "search",
        }
    ]


def test_context_without_thread_uses_existing_find_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    context = manager.get_context(
        "alice",
        agent_name="research",
    )

    assert context == "- [preferences] Prefers concise answers."
    assert manager._retriever.calls == [
        {
            "query": "profile preferences and prior decisions",
            "actor_peer": "research",
            "limit": 4,
            "filter": None,
            "session_id": None,
            "target_uri": [
                "viking://~/memories",
                "viking://~/peers/research/memories",
            ],
            "search_mode": "find",
        }
    ]


def test_unreachable_context_read_raise_aborts_dynamic_context_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unreachable_openviking_url: str,
) -> None:
    manager = _manager(
        tmp_path,
        monkeypatch,
        base_url=unreachable_openviking_url,
        timeout_seconds=0.1,
        failure_policy={"read": "raise"},
    )
    monkeypatch.setattr(
        "deerflow.agents.memory.get_memory_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.dynamic_context_middleware.resolve_runtime_user_id",
        lambda runtime: "alice",
    )
    middleware = DynamicContextMiddleware()
    state = {"messages": [HumanMessage("answer this", id="message-1")]}

    with pytest.raises(MemoryReadError) as exc_info:
        middleware.before_agent(state, None)

    assert exc_info.value.__cause__ is not None


def test_strict_scope_mismatch_uses_required_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(
        tmp_path,
        monkeypatch,
        failure_policy={"read": "raise"},
    )
    monkeypatch.setattr(
        "deerflow.agents.memory.get_memory_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.dynamic_context_middleware.resolve_runtime_user_id",
        lambda runtime: "bob",
    )
    middleware = DynamicContextMiddleware()
    state = {"messages": [HumanMessage("answer this", id="message-1")]}

    with pytest.raises(MemoryReadError, match="owner_user_id 'alice'") as exc_info:
        middleware.before_agent(state, None)
    with pytest.raises(MemoryReadError, match="owner_user_id 'alice'") as search_error:
        manager.search("preferences", user_id="bob", agent_name="research")

    for error in (exc_info.value, search_error.value):
        assert isinstance(error.__cause__, MemoryManagerError)
        assert "owner_user_id 'alice'" in str(error.__cause__)
    assert manager._retriever.calls == []


def test_unreachable_context_read_fail_open_returns_no_injected_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unreachable_openviking_url: str,
) -> None:
    manager = _manager(
        tmp_path,
        monkeypatch,
        base_url=unreachable_openviking_url,
        timeout_seconds=0.1,
        failure_policy={"read": "fail_open"},
    )
    monkeypatch.setattr(
        "deerflow.agents.memory.get_memory_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.dynamic_context_middleware.resolve_runtime_user_id",
        lambda runtime: "alice",
    )
    middleware = DynamicContextMiddleware()
    state = {"messages": [HumanMessage("answer this", id="message-1")]}

    assert manager.get_context("alice", agent_name="research") == ""
    update = middleware.before_agent(state, None)
    assert update is not None
    assert all(not str(message.id).endswith("__memory") for message in update["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("read_policy", ["raise", "fail_open"])
async def test_unreachable_async_context_read_honors_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unreachable_openviking_url: str,
    read_policy: str,
) -> None:
    manager = _manager(
        tmp_path,
        monkeypatch,
        base_url=unreachable_openviking_url,
        timeout_seconds=0.1,
        failure_policy={"read": read_policy},
    )

    if read_policy == "raise":
        with pytest.raises(MemoryReadError) as exc_info:
            await manager.aget_context("alice", agent_name="research")
        assert exc_info.value.__cause__ is not None
    else:
        assert await manager.aget_context("alice", agent_name="research") == ""


@pytest.mark.asyncio
async def test_unreachable_context_read_raise_aborts_async_dynamic_context_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unreachable_openviking_url: str,
) -> None:
    manager = _manager(
        tmp_path,
        monkeypatch,
        base_url=unreachable_openviking_url,
        timeout_seconds=0.1,
        failure_policy={"read": "raise"},
    )
    monkeypatch.setattr(
        "deerflow.agents.memory.get_memory_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.dynamic_context_middleware.resolve_runtime_user_id",
        lambda runtime: "alice",
    )
    middleware = DynamicContextMiddleware()
    state = {"messages": [HumanMessage("answer this", id="message-1")]}

    with pytest.raises(MemoryReadError) as exc_info:
        await middleware.abefore_agent(state, None)

    assert exc_info.value.__cause__ is not None


def test_manager_refuses_to_share_single_user_key_across_deerflow_users(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    with pytest.raises(MemoryManagerError, match="owner_user_id 'alice'"):
        manager.get_context("bob", agent_name="research")
    with pytest.raises(MemoryManagerError, match="owner_user_id 'alice'"):
        manager.add(
            "thread-1",
            [HumanMessage("private", id="h1")],
            user_id="bob",
            agent_name="research",
        )

    assert manager._retriever.calls == []
    assert manager._recorder.calls == []


def test_manager_records_only_unseen_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    messages = [
        HumanMessage("Remember Vim.", id="h1"),
        AIMessage("I will remember.", id="a1"),
    ]

    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )
    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )
    messages.append(HumanMessage("Also concise.", id="h2"))
    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )

    assert len(manager._recorder.calls) == 2
    assert manager._recorder.calls[0][1] == messages[:2]
    assert manager._recorder.calls[1][1] == messages[2:]
    assert all(call[2:] == ("research", "research") for call in manager._recorder.calls)


def test_partial_write_progress_is_not_resubmitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    messages = [
        HumanMessage("one", id="h1"),
        AIMessage("two", id="a1"),
        HumanMessage("three", id="h2"),
    ]
    manager._recorder.failures.append(_PartialWriteError(2))

    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )
    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )

    assert manager._recorder.calls[0][1] == messages
    assert manager._recorder.calls[1][1] == messages[2:]


def test_pending_commit_is_retried_without_resubmitting_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    messages = [HumanMessage("one", id="h1"), AIMessage("two", id="a1")]
    manager._recorder.failures.append(_PartialWriteError(len(messages), commit_pending=True))

    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )
    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )

    session_id = _session_id("alice", "research", "thread-1")
    assert manager._recorder.flushes == [(session_id, "research")]
    assert len(manager._recorder.calls) == 1


def test_search_maps_documents_without_custom_http_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    results = manager.search(
        "answer style",
        top_k=3,
        user_id="alice",
        agent_name="research",
        category="preferences",
    )

    assert results == [
        {
            "id": "viking://user/memories/preferences/style.md",
            "content": "Prefers concise answers.",
            "category": "preferences",
            "confidence": 0.91,
            "source": "viking://user/memories/preferences/style.md",
            "score": 0.91,
        }
    ]
    assert manager._retriever.calls[0]["filter"] == {
        "op": "must",
        "field": "category",
        "conds": ["preferences"],
    }
    assert manager._retriever.calls[0]["search_mode"] == "find"


def test_capture_keeps_tool_history_but_drops_hidden_injected_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    messages = [
        HumanMessage("question", id="h1"),
        AIMessage(
            "",
            id="a-tool",
            tool_calls=[
                {
                    "name": "search",
                    "args": {"query": "OpenViking"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        ),
        HumanMessage(
            "injected memory",
            id="hidden",
            additional_kwargs={"hide_from_ui": True},
        ),
        AIMessage("answer", id="a1"),
    ]

    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )

    assert manager._recorder.calls[0][1] == [
        messages[0],
        messages[1],
        messages[3],
    ]


def test_compacted_history_rebases_without_replaying_known_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(
        tmp_path,
        monkeypatch,
        max_seen_message_ids=16,
    )
    messages = [HumanMessage(f"message {index}", id=f"h{index}") for index in range(20)]
    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )
    compacted = [
        *messages[-8:],
        HumanMessage("new", id="h20"),
    ]

    manager.add(
        "thread-1",
        compacted,
        user_id="alice",
        agent_name="research",
    )

    assert manager._recorder.calls[1][1] == [compacted[-1]]


def test_failed_write_does_not_advance_capture_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager._recorder.failures.append(RuntimeError("unavailable"))
    messages = [HumanMessage("retry me", id="h1")]

    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )
    manager.add(
        "thread-1",
        messages,
        user_id="alice",
        agent_name="research",
    )

    assert [call[1] for call in manager._recorder.calls] == [
        messages,
        messages,
    ]


def test_corrupt_cursor_fails_closed_instead_of_replaying_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    session_id = _session_id("alice", "research", "thread-1")
    cursor = manager._state_path(session_id)
    cursor.parent.mkdir(parents=True)
    cursor.write_text("not-json", encoding="utf-8")

    with pytest.raises(MemoryManagerError, match="refusing unsafe replay"):
        manager.add(
            "thread-1",
            [HumanMessage("private", id="h1")],
            user_id="alice",
            agent_name="research",
        )

    assert manager._recorder.calls == []


def test_peer_mapping_is_stable_and_namespaces_are_disjoint() -> None:
    assert _canonical_peer_id(None, "deerflow") == "deerflow"
    assert _canonical_peer_id("Research", "deerflow") == "research"
    assert _canonical_peer_id("deerflow", "deerflow").startswith("df-agent-")
    assert _canonical_peer_id("-research", "deerflow").startswith("df-agent-")
    assert _canonical_peer_id("df-agent-custom", "deerflow").startswith("df-agent-")
    assert (
        len(
            {
                _canonical_peer_id(None, "deerflow"),
                _canonical_peer_id("deerflow", "deerflow"),
                _canonical_peer_id("-research", "deerflow"),
                _canonical_peer_id("df-agent-custom", "deerflow"),
            }
        )
        == 4
    )


def test_session_locks_do_not_accumulate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    lock = manager._session_lock("session-1")
    lock_ref = weakref.ref(lock)

    assert len(manager._session_locks) == 1
    del lock
    gc.collect()

    assert lock_ref() is None
    assert len(manager._session_locks) == 0


def test_shutdown_uses_existing_manager_lifecycle_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    assert manager.shutdown_flush(1.0) is True
    assert manager._recorder.closed is True
    assert manager._recorder.client.closed is True
    assert manager.shutdown_flush(1.0) is True


def test_shutdown_timeout_closes_resources_after_active_write_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    original_record = manager._recorder.record

    def blocking_record(*args: Any, **kwargs: Any) -> object:
        started.set()
        assert release.wait(2.0)
        return original_record(*args, **kwargs)

    manager._recorder.record = blocking_record
    writer = threading.Thread(
        target=manager.add,
        args=("thread-1", [HumanMessage("hello", id="h1")]),
        kwargs={"user_id": "alice", "agent_name": "research"},
    )
    writer.start()
    assert started.wait(1.0)

    assert manager.shutdown_flush(0.0) is False
    assert manager._recorder.closed is False

    release.set()
    writer.join(2.0)

    assert not writer.is_alive()
    assert manager._recorder.closed is True
    assert manager._recorder.client.closed is True
    assert manager.shutdown_flush(1.0) is True


@pytest.mark.asyncio
async def test_async_operations_run_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    original_add = OpenVikingMemoryManager.add

    def recording_add(
        self: OpenVikingMemoryManager,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        worker_threads.append(threading.get_ident())
        original_add(self, *args, **kwargs)

    monkeypatch.setattr(OpenVikingMemoryManager, "add", recording_add)

    await manager.aadd(
        "thread-1",
        [HumanMessage("hello", id="h1")],
        user_id="alice",
        agent_name="research",
    )

    assert worker_threads
    assert worker_threads[0] != event_loop_thread


def test_capture_cursor_contains_no_message_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    manager.add(
        "thread-1",
        [HumanMessage("very private text", id="h1")],
        user_id="alice",
        agent_name="research",
    )

    cursor = next((tmp_path / "openviking" / "sessions").glob("*.json"))
    serialized = cursor.read_text(encoding="utf-8")
    state = json.loads(serialized)

    assert "very private text" not in serialized
    assert state["submitted_prefix_count"] == 1


def test_get_memory_lists_and_reads_only_the_actors_own_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    own = "viking://~/peers/research/memories/style.md"
    other = "viking://~/peers/other/memories/secret.md"
    user_level = "viking://~/memories/shared.md"
    manager._client.files = {
        own: "Prefers concise answers.",
        other: "Someone else's memory.",
        user_level: "Shared user-level memory.",
    }

    document = manager.get_memory(user_id="alice", agent_name="research")

    assert document == {"facts": [{"id": own, "content": "Prefers concise answers.", "source": own}]}
    assert manager._client.reads == [own]


def test_delete_fact_removes_only_an_own_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    uri = "viking://~/peers/research/memories/style.md"
    remaining = "viking://~/peers/research/memories/other.md"
    manager._client.files = {remaining: "Still here."}

    document = manager.delete_fact(uri, user_id="alice", agent_name="research")

    assert manager._client.removals == [(uri, False)]
    # The response is the remaining document, not an empty store.
    assert [fact["id"] for fact in document["facts"]] == [remaining]


def test_delete_fact_refuses_a_uri_outside_the_actors_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    with pytest.raises(MemoryManagerError, match="outside this actor's scope"):
        manager.delete_fact("viking://~/peers/other/memories/secret.md", user_id="alice", agent_name="research")
    with pytest.raises(MemoryManagerError, match="outside this actor's scope"):
        manager.delete_fact("viking://~/memories/shared.md", user_id="alice", agent_name="research")
    assert manager._client.removals == []


def test_clear_memory_clears_only_the_actors_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    manager.clear_memory(user_id="alice", agent_name="research")

    assert manager._client.removals == [("viking://~/peers/research/memories", True)]


def test_get_memory_without_an_agent_covers_the_whole_actor_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    user_level = "viking://~/memories/summary.md"
    agent_bucket = "viking://~/peers/research/memories/style.md"
    other_agent = "viking://~/peers/other/memories/note.md"
    manager._client.files = {
        user_level: "User-level summary.",
        agent_bucket: "Research fact.",
        other_agent: "Other agent fact.",
    }

    # The gateway's management endpoints call without an agent name; recall
    # reads the user-level root plus every peer bucket, so those are what it
    # must list.
    document = manager.get_memory(user_id="alice")

    assert {fact["id"] for fact in document["facts"]} == {user_level, agent_bucket, other_agent}


def test_unscoped_memory_refuses_a_different_deerflow_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager._client.files = {"viking://~/memories/secret.md": "Alice only"}

    with pytest.raises(MemoryManagerError, match="owner_user_id 'alice'"):
        manager.get_memory(user_id="bob")
    with pytest.raises(MemoryManagerError, match="owner_user_id 'alice'"):
        manager.clear_memory(user_id="bob")
    with pytest.raises(MemoryManagerError, match="owner_user_id 'alice'"):
        manager.delete_fact("viking://~/memories/secret.md", user_id="bob")

    assert manager._client.reads == []
    assert manager._client.removals == []


def test_clear_memory_without_an_agent_covers_every_recall_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager._client.files = {
        "viking://~/peers/research/memories/fact.md": "fact",
        "viking://~/peers/research/resources/keep.md": "resource",
        "viking://~/peers/df-shared-team/memories/keep.md": "curated",
    }

    manager.clear_memory(user_id="alice")

    assert manager._client.removals == [("viking://~/memories", True), ("viking://~/peers/research/memories", True)]


@pytest.mark.parametrize("uri", ["viking://~/peers/research/resources/keep.md", "viking://~/peers/df-shared-team/memories/keep.md", "viking://~/peers/research/memories/%2e%2e/secret.md"])
def test_unscoped_delete_refuses_non_memory_and_encoded_paths(tmp_path, monkeypatch, official_integration, uri):
    manager = _manager(tmp_path, monkeypatch)
    manager._client.files = {uri: "keep"}
    with pytest.raises(MemoryManagerError):
        manager.delete_fact(uri, user_id="alice")
    assert manager._client.removals == []


def test_unscoped_listing_excludes_non_memory_and_curated_buckets(tmp_path, monkeypatch, official_integration):
    manager = _manager(tmp_path, monkeypatch)
    own = "viking://~/peers/research/memories/fact.md"
    manager._client.files = {
        own: "fact",
        "viking://~/peers/research/resources/keep.md": "resource",
        "viking://~/peers/df-shared-team/memories/keep.md": "curated",
    }
    assert [fact["id"] for fact in manager.get_memory(user_id="alice")["facts"]] == [own]
    assert manager._client.reads == [own]


def test_listing_rejects_out_of_scope_glob_results(
    tmp_path,
    monkeypatch,
    official_integration,
):
    manager = _manager(tmp_path, monkeypatch)
    foreign = "viking://~/peers/other/memories/secret.md"
    manager._client.files = {foreign: "private"}
    monkeypatch.setattr(manager._client, "glob", lambda **kwargs: [foreign])
    with pytest.raises(MemoryManagerError):
        manager.get_memory(user_id="alice", agent_name="research")
    assert manager._client.reads == []


def test_governance_accepts_concrete_credential_uris_but_not_foreign_ids(tmp_path, monkeypatch, official_integration):
    manager = _manager(tmp_path, monkeypatch)
    own = "viking://user/ov-alice/peers/research/memories/fact.md"
    manager._client.files = {own: "fact"}
    monkeypatch.setattr(manager._client, "ls", lambda **kwargs: ["viking://user/ov-alice/peers/research"])
    monkeypatch.setattr(manager._client, "glob", lambda **kwargs: [own] if "/research/" in kwargs["uri"] else [])
    assert [fact["id"] for fact in manager.get_memory(user_id="alice")["facts"]] == [own]
    with pytest.raises(MemoryManagerError):
        manager.delete_fact(own.replace("ov-alice", "ov-bob"), user_id="alice")
    assert manager._client.removals == []
    manager.delete_fact(own, user_id="alice")
    assert manager._client.removals == [(own, False)]


@pytest.mark.parametrize("scope,expected_peer", [("instance", None), ("requester", "alice")])
def test_governance_preserves_actor_scope(tmp_path, monkeypatch, official_integration, scope, expected_peer):
    manager = _manager(tmp_path, monkeypatch, memory_scope=scope)
    calls = []
    original = manager._call_openviking

    def record(client, method, **kwargs):
        calls.append((method, _ACTOR_PEER.get()))
        return original(client, method, **kwargs)

    manager._call_openviking = record
    # user_id is the storage owner. Requester scope must not treat it as the
    # trusted requester, and must not keep an ambient actor peer either.
    requester_id = "alice" if scope == "requester" else None
    with _use_actor_peer("unrelated"):
        if scope == "requester":
            with pytest.raises(RequesterScopeError):
                manager.get_memory(user_id="alice")
        manager.get_memory(user_id="alice", requester_id=requester_id)
        manager.clear_memory(user_id="alice", requester_id=requester_id)
    assert calls and all(peer == expected_peer for _, peer in calls)


def test_get_memory_surfaces_an_enumeration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager._client.glob_error = RuntimeError("openviking unreachable")

    # An unreachable backend must fail, not look like an empty memory.
    with pytest.raises(RuntimeError, match="unreachable"):
        manager.get_memory(user_id="alice")


def test_delete_fact_refuses_a_traversal_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    with pytest.raises(MemoryManagerError, match="suspicious memory URI"):
        manager.delete_fact(
            "viking://~/peers/research/memories/../../other/secret.md",
            user_id="alice",
            agent_name="research",
        )
    assert manager._client.removals == []


def test_memory_scope_defaults_to_instance_and_is_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENVIKING_API_KEY", "secret")
    config = OpenVikingConfig.from_backend_config(_backend_config(tmp_path))
    assert config.memory_scope == "instance"
    assert config.shared_peer_id == ""

    with pytest.raises(ValueError, match="memory_scope"):
        OpenVikingConfig.from_backend_config(_backend_config(tmp_path, memory_scope="tenant"))


def test_shared_peer_id_is_requester_only_and_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENVIKING_API_KEY", "secret")
    with pytest.raises(ValueError, match="shared_peer_id is only used"):
        OpenVikingConfig.from_backend_config(_backend_config(tmp_path, shared_peer_id="df-shared-finance"))
    with pytest.raises(ValueError, match="shared_peer_id"):
        OpenVikingConfig.from_backend_config(_backend_config(tmp_path, memory_scope="requester", shared_peer_id="Bad Name"))
    # The shared bucket must live in its own namespace, otherwise a requester
    # id could alias it and land private writes in a bucket everyone reads.
    for colliding in ("finance-shared", "df-user-custom", "df-agent-custom"):
        with pytest.raises(ValueError, match="shared_peer_id must use the reserved prefix"):
            OpenVikingConfig.from_backend_config(_backend_config(tmp_path, memory_scope="requester", shared_peer_id=colliding))
    config = OpenVikingConfig.from_backend_config(_backend_config(tmp_path, memory_scope="requester", shared_peer_id="df-shared-finance"))
    assert config.shared_peer_id == "df-shared-finance"


def test_requester_peer_id_is_stable_and_namespaced() -> None:
    assert _requester_peer_id("alice") == "alice"
    assert _requester_peer_id("Alice") == "alice"
    assert _requester_peer_id("user@example.com").startswith("df-user-")
    # Every "df-" namespace is adapter-owned, so a requester id that looks like
    # one is hashed instead of aliasing an agent peer or the shared bucket.
    assert _requester_peer_id("df-agent-custom").startswith("df-user-")
    assert _requester_peer_id("df-user-custom").startswith("df-user-")
    assert _requester_peer_id("df-shared-finance").startswith("df-user-")
    assert _requester_peer_id("has space").startswith("df-user-")


def test_instance_scope_keeps_the_user_level_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)

    manager.get_context("alice", agent_name="research")

    assert manager._retriever.calls[0]["target_uri"] == [
        "viking://~/memories",
        "viking://~/peers/research/memories",
    ]


def test_requester_scope_partitions_memory_per_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch, memory_scope="requester")

    manager.get_context("alice", agent_name="research", requester_id="alice")
    manager.get_context("bob", agent_name="research", requester_id="bob")

    assert manager._retriever.calls[0]["target_uri"] == ["viking://~/peers/alice/memories"]
    assert manager._retriever.calls[1]["target_uri"] == ["viking://~/peers/bob/memories"]
    assert "viking://~/memories" not in manager._retriever.calls[0]["target_uri"]
    assert manager._retriever.calls[0]["actor_peer"] == "alice"
    assert manager._retriever.calls[1]["actor_peer"] == "bob"

    # The write side must partition too: the recorder is handed the requester
    # as both peer and actor, under a per-requester session id.
    manager.add("thread-1", [HumanMessage("alice fact", id="h1")], user_id="alice", agent_name="research", requester_id="alice")
    manager.add("thread-2", [HumanMessage("bob fact", id="h2")], user_id="bob", agent_name="research", requester_id="bob")

    alice_write, bob_write = manager._recorder.calls[0], manager._recorder.calls[1]
    assert (alice_write[2], alice_write[3]) == ("alice", "alice")
    assert (bob_write[2], bob_write[3]) == ("bob", "bob")
    assert alice_write[0] != bob_write[0]


def test_requester_scope_can_read_a_curated_shared_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch, memory_scope="requester", shared_peer_id="df-shared-finance")

    manager.get_context("bob", agent_name="research", requester_id="bob")

    assert manager._retriever.calls[0]["target_uri"] == [
        "viking://~/peers/bob/memories",
        "viking://~/peers/df-shared-finance/memories",
    ]


def test_requester_scope_fails_closed_without_a_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch, memory_scope="requester")

    # The requester comes from its own trusted channel; user_id (the storage
    # owner) must not stand in for it, and "default" is never a requester.
    assert manager.get_context("alice", agent_name="research") == ""
    assert manager.get_context("alice", agent_name="research", requester_id="default") == ""

    manager.add(
        "thread-1",
        [HumanMessage("private", id="h1")],
        user_id="alice",
        agent_name="research",
    )
    assert manager._retriever.calls == []
    assert manager._recorder.calls == []


# --------------------------------------------------------------------------
# Delete propagation: retrieval reads the index, so an entry whose file was
# already deleted (a delete that raced a pending index task, or an orphan that
# a rebuild keeps) would still recall the deleted content.
# --------------------------------------------------------------------------

_STALE_URI = "viking://user/alice/memories/preferences/style.md"
_STALE_ROOT_URI = "viking://~/memories/preferences/style.md"
_STALE_PARENT = "viking://~/memories/preferences"


def _document(uri: str | None, content: str = "Prefers concise answers.") -> Document:
    metadata: dict[str, Any] = {
        "openviking_category": "preferences",
        "openviking_score": 0.91,
    }
    if uri is not None:
        metadata["openviking_uri"] = uri
    return Document(page_content=content, metadata=metadata)


def _return_documents(*documents: Document) -> Any:
    """Make every retriever copy return exactly *documents*."""

    def invoke(self: _Retriever, query: str) -> list[Document]:
        return list(documents)

    return invoke


def test_context_drops_recalled_entries_whose_file_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(_Retriever, "invoke", _return_documents(_document(_STALE_URI)))

    # The index still returns the entry; the store no longer has the file.
    assert manager._client.files == {}

    assert manager.get_context("alice", agent_name="research") == ""
    assert manager._client.ls_calls == [_STALE_PARENT]


def test_context_keeps_recalled_entries_whose_file_still_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(_Retriever, "invoke", _return_documents(_document(_STALE_URI)))
    manager._client.files = {_STALE_ROOT_URI: "Prefers concise answers."}

    assert manager.get_context("alice", agent_name="research") == ("- [preferences] Prefers concise answers.")
    assert manager._client.ls_calls == [_STALE_PARENT]


def test_search_drops_recalled_entries_whose_file_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(_Retriever, "invoke", _return_documents(_document(_STALE_URI)))

    assert manager.search("answer style", top_k=3, user_id="alice", agent_name="research") == []


@pytest.mark.parametrize("read_policy", ["fail_open", "raise"])
@pytest.mark.parametrize("operation", ["context", "search"])
def test_recall_does_not_expose_an_entry_when_existence_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
    read_policy: str,
    operation: str,
) -> None:
    manager = _manager(tmp_path, monkeypatch, failure_policy={"read": read_policy})
    monkeypatch.setattr(_Retriever, "invoke", _return_documents(_document(_STALE_URI)))
    manager._client.glob_error = RuntimeError("listing unavailable")

    def recall():
        if operation == "context":
            return manager.get_context("alice", agent_name="research")
        return manager.search("answer style", user_id="alice", agent_name="research")

    # fail_open means continue without unverified content, not reuse stale
    # index text whose backing file may already have been deleted.
    if read_policy == "raise":
        with pytest.raises(MemoryReadError, match="verification"):
            recall()
    else:
        assert recall() == ("" if operation == "context" else [])

    # Skipping a recall does not erase memory: a later verified read recovers.
    manager._client.glob_error = None
    manager._client.files[_STALE_ROOT_URI] = "Prefers concise answers."
    assert recall()
    assert manager._client.removals == []


def test_recall_keeps_an_entry_without_a_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(_Retriever, "invoke", _return_documents(_document(None)))

    assert manager.get_context("alice", agent_name="research") == ("- [preferences] Prefers concise answers.")
    assert manager._client.ls_calls == []


def test_recall_skips_the_existence_check_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    official_integration: None,
) -> None:
    manager = _manager(
        tmp_path,
        monkeypatch,
        retrieval={
            "top_k": 4,
            "max_injection_chars": 1_000,
            "injection_query": "profile preferences and prior decisions",
            "drop_deleted_entries": False,
        },
    )
    monkeypatch.setattr(_Retriever, "invoke", _return_documents(_document(_STALE_URI)))

    assert manager.get_context("alice", agent_name="research") == ("- [preferences] Prefers concise answers.")
    assert manager._client.ls_calls == []


def test_drop_deleted_entries_defaults_to_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENVIKING_API_KEY", "user-key")

    config = OpenVikingConfig.from_backend_config(_backend_config(tmp_path))

    assert config.drop_deleted_entries is True
