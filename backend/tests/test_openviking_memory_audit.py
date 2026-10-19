"""Access trail for the OpenViking memory backend.

Memory governance asks for a record of *who* touched memory, under which scope,
and *how much* came back. The content itself is deliberately never part of that
record: memory bodies can hold personal data, and a trail is not a second copy
of the store.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import test_openviking_memory_backend as backend_fakes
from langchain_core.messages import AIMessage, HumanMessage

import deerflow.agents.memory.backends.openviking.openviking_manager as openviking_manager_module

AUDIT_LOGGER = "deerflow.agents.memory.backends.openviking.audit"

SECRET_CONTENT = "本季度的报价是 128000 元，客户还在等回复"


@pytest.fixture
def openviking_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the fake OpenViking integration the backend suite uses.

    The fixture itself lives in the other test module, which pytest would not
    resolve from here; the wiring is copied instead of imported.
    """

    monkeypatch.setattr(
        openviking_manager_module,
        "_load_official_integration",
        lambda: {
            "OpenVikingCommitPolicy": backend_fakes._CommitPolicy,
            "OpenVikingPartialWriteError": backend_fakes._PartialWriteError,
            "OpenVikingRetriever": backend_fakes._Retriever,
            "OpenVikingSessionRecorder": backend_fakes._Recorder,
            "use_actor_peer": backend_fakes._use_actor_peer,
            "call_openviking": lambda client, method, /, **kwargs: getattr(client, method)(**kwargs),
        },
    )


@pytest.fixture
def trail(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    return caplog


def access_trails(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    """Parse the memory access records the manager emitted."""

    trails: list[dict[str, str]] = []
    for record in caplog.records:
        message = record.getMessage()
        if not message.startswith("memory access "):
            continue
        entry: dict[str, str] = {}
        for token in message.split()[2:]:  # "memory access mode=… scope=…"
            if "=" in token:
                key, value = token.split("=", 1)
                entry[key] = value
        trails.append(entry)
    return trails


def one_trail(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    trails = access_trails(caplog)
    assert len(trails) == 1, f"expected exactly one access trail, got {trails}"
    return trails[0]


def test_injection_trails_the_actor_scope_and_how_much_came_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openviking_fakes: None,
    trail: pytest.LogCaptureFixture,
) -> None:
    manager = backend_fakes._manager(tmp_path, monkeypatch)

    context = manager.get_context("alice", agent_name="research", thread_id="thread-1")

    assert "Prefers concise answers." in context
    entry = one_trail(trail)
    assert entry["mode"] == "injection"
    assert entry["user"] == "alice"
    assert entry["agent"] == "research"
    assert entry["scope"] == "instance"
    assert entry["hits"] == "1"
    assert entry["thread"] == "thread-1"


def test_search_trails_the_requester_of_a_requester_scoped_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openviking_fakes: None,
    trail: pytest.LogCaptureFixture,
) -> None:
    manager = backend_fakes._manager(tmp_path, monkeypatch, memory_scope="requester")

    manager.search("answer style", user_id="alice", agent_name="research", requester_id="r1")

    entry = one_trail(trail)
    assert entry["mode"] == "search"
    assert entry["requester"] == "r1"
    assert entry["scope"] == "requester"
    assert entry["hits"] == "1"
    # Entry paths are not part of the trail: the platform's log redaction
    # collapses them anyway, so the trail records how much came back.
    assert "uris" not in entry


def test_capture_trails_the_turn_without_its_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openviking_fakes: None,
    trail: pytest.LogCaptureFixture,
) -> None:
    manager = backend_fakes._manager(tmp_path, monkeypatch)

    manager.add(
        "thread-1",
        [HumanMessage(SECRET_CONTENT, id="h1"), AIMessage("好的", id="a1")],
        user_id="alice",
        agent_name="research",
    )

    entry = one_trail(trail)
    assert entry["mode"] == "capture"
    assert entry["hits"] == "2"
    assert entry["thread"] == "thread-1"
    # An access trail is not a transcript: nothing the user said may appear.
    assert SECRET_CONTENT not in trail.text
    assert "好的" not in trail.text


def test_manage_operations_trail_every_mode_without_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openviking_fakes: None,
    trail: pytest.LogCaptureFixture,
) -> None:
    manager = backend_fakes._manager(tmp_path, monkeypatch)

    _document, fact_id = manager.create_fact("Fact one", user_id="alice", agent_name="research")
    manager.update_fact(fact_id, "Fact two", user_id="alice", agent_name="research")
    manager.delete_fact(fact_id, user_id="alice", agent_name="research")
    manager.clear_memory(user_id="alice", agent_name="research")

    # create/update/delete answer with the memory document, so a "list" trail
    # follows each of them; that ordering is part of what makes it auditable.
    assert [entry["mode"] for entry in access_trails(trail)] == [
        "create",
        "list",
        "update",
        "list",
        "delete",
        "list",
        "clear",
    ]
    assert "Fact one" not in trail.text
    assert "Fact two" not in trail.text


def test_list_trails_the_entries_it_touched_whatever_the_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openviking_fakes: None,
    trail: pytest.LogCaptureFixture,
) -> None:
    manager = backend_fakes._manager(tmp_path, monkeypatch)
    # The employee's bucket: a scoped read walks that directory, not the
    # user-level root.
    bucket = "viking://~/peers/research/memories"
    count = 25
    for index in range(count):
        manager._client.files[f"{bucket}/fact-{index}.md"] = f"content {index}"

    document = manager.get_memory(user_id="alice", agent_name="research")

    entry = one_trail(trail)
    assert entry["mode"] == "list"
    assert entry["hits"] == str(count)
    assert len(document["facts"]) == count
    assert "content 0" not in trail.text, "the entries themselves stay out of the trail"


def test_audit_survives_a_recall_that_returned_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openviking_fakes: None,
    trail: pytest.LogCaptureFixture,
) -> None:
    manager = backend_fakes._manager(tmp_path, monkeypatch)
    monkeypatch.setattr(backend_fakes._Retriever, "invoke", lambda self, query: [])

    context = manager.get_context("alice", agent_name="research")

    assert context == ""
    entry = one_trail(trail)
    assert entry["mode"] == "injection"
    assert entry["hits"] == "0"
