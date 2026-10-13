"""The trusted requester must reach memory as explicit run-context data.

The ContextVar alone is not enough: LangGraph runs the model node and the
before/after-agent nodes in different execution contexts, so a value set inside
one node does not propagate. These tests pin the carrier contract that the
memory read/write paths depend on.
"""

from __future__ import annotations

from dataclasses import dataclass

from deerflow.runtime.user_context import (
    get_requester_id,
    requester_from_runtime,
    reset_requester_id,
    set_requester_id,
)


@dataclass
class _Runtime:
    context: object = None


def test_prefers_the_explicit_run_context_field() -> None:
    token = set_requester_id("ignored-contextvar")
    try:
        runtime = _Runtime(context={"requester_id": "alice"})
        assert requester_from_runtime(runtime) == "alice"
    finally:
        reset_requester_id(token)


def test_falls_back_to_the_task_local_contextvar() -> None:
    token = set_requester_id("alice")
    try:
        # No field on the run context at all: the ContextVar is the fallback.
        assert requester_from_runtime(_Runtime(context={})) == "alice"
        assert requester_from_runtime(None) == "alice"
    finally:
        reset_requester_id(token)


def test_unresolved_is_none_and_never_invented() -> None:
    token = set_requester_id(None)
    try:
        assert requester_from_runtime(_Runtime(context={"requester_id": None})) is None
        assert requester_from_runtime(_Runtime(context={})) is None
        assert requester_from_runtime(None) is None
        assert get_requester_id() is None
    finally:
        reset_requester_id(token)


def test_requester_kwargs_only_reaches_flagged_backends() -> None:
    from deerflow.agents.memory.manager import requester_kwargs

    class Plain:
        pass

    class Scoped:
        supports_requester_scope = True

    # Backends without the capability never receive the keyword, so their
    # signatures stay unchanged.
    assert requester_kwargs(Plain(), "alice") == {}
    assert requester_kwargs(Scoped(), "alice") == {"requester_id": "alice"}
    assert requester_kwargs(Scoped(), None) == {"requester_id": None}
