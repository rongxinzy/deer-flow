import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from _router_auth_helpers import call_unwrapped, make_authed_test_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send
from test_openviking_memory_backend import _manager as _openviking_manager
from test_openviking_memory_backend import official_integration as official_integration

from app.gateway.internal_auth import INTERNAL_OWNER_USER_ID_HEADER_NAME, INTERNAL_SYSTEM_ROLE
from app.gateway.routers import memory
from deerflow.agents.memory import MemoryConflictError, MemoryCorruptionError, MemoryManagerError, MemoryReadError
from deerflow.agents.memory.backends.deermem.deer_mem import DeerMem
from deerflow.agents.memory.backends.openviking.openviking_manager import OpenVikingMemoryManager, RequesterScopeError
from deerflow.runtime.user_context import DEFAULT_USER_ID, reset_requester_id, set_requester_id


@pytest.mark.parametrize("method,path", [("delete", "/api/memory"), ("delete", "/api/memory/facts/fact")])
@pytest.mark.parametrize("error,status", [(MemoryConflictError("conflict"), 409), (MemoryCorruptionError("corrupt"), 500)])
def test_memory_delete_preserves_specialized_errors(method, path, error, status):
    app = make_authed_test_app()
    app.include_router(memory.router)
    manager = MagicMock()
    manager.clear_memory.side_effect = error
    manager.delete_fact.side_effect = error
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=manager), TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == status


def test_memory_delete_accepts_openviking_uri_fact_ids():
    app = make_authed_test_app()
    app.include_router(memory.router)
    manager = MagicMock()
    manager.delete_fact.return_value = {"facts": []}
    uri = "viking://~/peers/research/memories/fact.md"
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=manager), TestClient(app) as client:
        response = client.delete("/api/memory/facts/" + quote(uri, safe=""))
    assert response.status_code == 200
    assert manager.delete_fact.call_args.args == (uri,)


def _sample_memory(facts: list[dict] | None = None) -> dict:
    return {
        "version": "1.0",
        "lastUpdated": "2026-03-26T12:00:00Z",
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentMonths": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": facts or [],
    }


# ── export ─────────────────────────────────────────────────────────────────


def test_export_memory_route_returns_current_memory() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    exported_memory = _sample_memory(facts=[{"id": "fact_export", "content": "User prefers concise responses.", "category": "preference", "confidence": 0.9, "createdAt": "2026-03-20T00:00:00Z", "source": "thread-1"}])

    mock_mgr = MagicMock()
    mock_mgr.get_memory.return_value = exported_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.get("/api/memory/export")
    assert response.status_code == 200
    assert response.json()["facts"] == exported_memory["facts"]


def test_get_memory_route_offloads_manager_call_from_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    called_from: list[int] = []
    manager = MagicMock()

    def get_memory(*, user_id: str) -> dict:
        called_from.append(threading.get_ident())
        return _sample_memory()

    manager.get_memory.side_effect = get_memory
    request = SimpleNamespace()
    with (
        patch("app.gateway.routers.memory.get_memory_manager", return_value=manager),
        patch("app.gateway.routers.memory._resolve_memory_user_id", return_value="user-1"),
    ):
        response = asyncio.run(call_unwrapped(memory.get_memory, request))

    assert response.facts == []
    assert called_from and called_from[0] != event_loop_thread


def test_export_memory_route_preserves_source_error() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    exported_memory = _sample_memory(
        facts=[
            {
                "id": "fact_correction",
                "content": "Use make dev for local development.",
                "category": "correction",
                "confidence": 0.95,
                "createdAt": "2026-03-20T00:00:00Z",
                "source": "thread-1",
                "sourceError": "The agent previously suggested npm start.",
            }
        ]
    )

    mock_mgr = MagicMock()
    mock_mgr.get_memory.return_value = exported_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.get("/api/memory/export")
    assert response.status_code == 200
    assert response.json()["facts"][0]["sourceError"] == "The agent previously suggested npm start."


# ── import ─────────────────────────────────────────────────────────────────


def test_import_memory_route_returns_imported_memory() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    imported_memory = _sample_memory(facts=[{"id": "fact_import", "content": "User works on DeerFlow.", "category": "context", "confidence": 0.87, "createdAt": "2026-03-20T00:00:00Z", "source": "manual"}])

    mock_mgr = MagicMock()
    mock_mgr.import_memory.return_value = imported_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.post("/api/memory/import", json=imported_memory)
    assert response.status_code == 200
    assert response.json()["facts"] == imported_memory["facts"]


def test_import_route_without_agent_name_persists_default_bucket_markdown(tmp_path) -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    manager = DeerMem(backend_config={"storage_path": str(tmp_path)})
    imported_memory = _sample_memory(
        facts=[
            {
                "id": "fact_gateway_import",
                "content": "Gateway imports use the default agent bucket.",
                "category": "context",
                "confidence": 0.9,
                "createdAt": "2026-07-21T00:00:00Z",
                "source": "import",
            }
        ]
    )

    with (
        patch("app.gateway.routers.memory.get_memory_manager", return_value=manager),
        patch("app.gateway.routers.memory.get_effective_user_id", return_value="alice"),
        TestClient(app) as client,
    ):
        response = client.post("/api/memory/import", json=imported_memory)

    assert response.status_code == 200
    assert [fact["id"] for fact in response.json()["facts"]] == ["fact_gateway_import"]
    facts_root = tmp_path / "users" / "alice" / "agents" / "__default__" / "facts"
    assert [path.stem for path in facts_root.glob("**/*.md")] == ["fact_gateway_import"]


def test_import_memory_route_preserves_source_error() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    imported_memory = _sample_memory(
        facts=[
            {
                "id": "fact_correction",
                "content": "Use make dev for local development.",
                "category": "correction",
                "confidence": 0.95,
                "createdAt": "2026-03-20T00:00:00Z",
                "source": "thread-1",
                "sourceError": "The agent previously suggested npm start.",
            }
        ]
    )

    mock_mgr = MagicMock()
    mock_mgr.import_memory.return_value = imported_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.post("/api/memory/import", json=imported_memory)
    assert response.status_code == 200
    assert response.json()["facts"][0]["sourceError"] == "The agent previously suggested npm start."


# ── clear ──────────────────────────────────────────────────────────────────


def test_clear_memory_route_returns_cleared_memory() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    mock_mgr = MagicMock()
    mock_mgr.clear_memory.return_value = _sample_memory()
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.delete("/api/memory")
    assert response.status_code == 200
    assert response.json()["facts"] == []


# ── fact CRUD (normal / error) ─────────────────────────────────────────────


def test_create_memory_fact_route_returns_updated_memory() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    updated_memory = _sample_memory(facts=[{"id": "fact_new", "content": "User prefers concise code reviews.", "category": "preference", "confidence": 0.88, "createdAt": "2026-03-20T00:00:00Z", "source": "manual"}])

    mock_mgr = MagicMock()
    mock_mgr.create_fact.return_value = (updated_memory, "fact_new")
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.post("/api/memory/facts", json={"content": "User prefers concise code reviews.", "category": "preference", "confidence": 0.88})
    assert response.status_code == 200
    assert response.json()["facts"] == updated_memory["facts"]


def test_create_memory_fact_route_maps_conflict_to_409() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    mock_mgr = MagicMock()
    mock_mgr.create_fact.side_effect = MemoryConflictError("stale write")

    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.post("/api/memory/facts", json={"content": "fact"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Memory changed concurrently; reload and retry."


def test_create_memory_fact_route_maps_duplicate_to_409() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    mock_mgr = MagicMock()
    mock_mgr.create_fact.side_effect = ValueError("Duplicate fact")

    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.post("/api/memory/facts", json={"content": "fact"})

    assert response.status_code == 409
    assert response.json()["detail"] == "A fact with the same content already exists."


def test_get_memory_route_maps_corruption_to_stable_500() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    mock_mgr = MagicMock()
    mock_mgr.get_memory.side_effect = MemoryCorruptionError("private path and parser detail")

    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.get("/api/memory")

    assert response.status_code == 500
    assert response.json()["detail"] == "Stored memory data is corrupted."


def test_delete_memory_fact_route_returns_updated_memory() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    updated_memory = _sample_memory(facts=[{"id": "fact_keep", "content": "User likes Python", "category": "preference", "confidence": 0.9, "createdAt": "2026-03-20T00:00:00Z", "source": "thread-1"}])

    mock_mgr = MagicMock()
    mock_mgr.delete_fact.return_value = updated_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.delete("/api/memory/facts/fact_delete")
    assert response.status_code == 200
    assert response.json()["facts"] == updated_memory["facts"]


def test_delete_memory_fact_route_returns_404_for_missing_fact() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    mock_mgr = MagicMock()
    mock_mgr.delete_fact.side_effect = KeyError("fact_missing")
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.delete("/api/memory/facts/fact_missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "Memory fact 'fact_missing' not found."


def test_update_memory_fact_route_returns_updated_memory() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    updated_memory = _sample_memory(facts=[{"id": "fact_edit", "content": "User prefers spaces", "category": "workflow", "confidence": 0.91, "createdAt": "2026-03-20T00:00:00Z", "source": "manual"}])

    mock_mgr = MagicMock()
    mock_mgr.update_fact.return_value = updated_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.patch("/api/memory/facts/fact_edit", json={"content": "User prefers spaces", "category": "workflow", "confidence": 0.91})
    assert response.status_code == 200
    assert response.json()["facts"] == updated_memory["facts"]


def test_settings_fact_crud_without_agent_name_uses_default_agent(tmp_path) -> None:
    """The current Settings API sends no agent_name; it must remain usable."""
    app = make_authed_test_app()
    app.include_router(memory.router)
    memory_path = tmp_path / "users" / "alice" / "memory.json"
    memory_path.parent.mkdir(parents=True)
    legacy = _sample_memory(
        facts=[
            {
                "id": "fact_legacy",
                "content": "Legacy global fact",
                "category": "context",
                "confidence": 0.9,
                "createdAt": "2026-03-20T00:00:00Z",
                "source": "manual",
            }
        ]
    )
    memory_path.write_text(json.dumps(legacy), encoding="utf-8")
    manager = DeerMem(backend_config={"storage_path": str(tmp_path)})

    with (
        patch("app.gateway.routers.memory.get_memory_manager", return_value=manager),
        patch("app.gateway.routers.memory.get_effective_user_id", return_value="alice"),
        TestClient(app) as client,
    ):
        fetched = client.get("/api/memory")
        assert fetched.status_code == 200
        assert [fact["content"] for fact in fetched.json()["facts"]] == ["Legacy global fact"]

        exported = client.get("/api/memory/export")
        assert exported.status_code == 200
        assert [fact["content"] for fact in exported.json()["facts"]] == ["Legacy global fact"]

        created = client.post("/api/memory/facts", json={"content": "Project uses Python", "category": "context", "confidence": 0.8})
        assert created.status_code == 200
        assert all(isinstance(fact["source"], str) for fact in created.json()["facts"])
        fact_id = next(fact["id"] for fact in created.json()["facts"] if fact["content"] == "Project uses Python")

        updated = client.patch(f"/api/memory/facts/{fact_id}", json={"content": "Project uses Python 3.12"})
        assert updated.status_code == 200
        assert updated.json()["facts"][0]["content"] == "Project uses Python 3.12"

        deleted = client.delete(f"/api/memory/facts/{fact_id}")
        assert deleted.status_code == 200
        assert [fact["id"] for fact in deleted.json()["facts"]] == ["fact_legacy"]

        deleted_legacy = client.delete("/api/memory/facts/fact_legacy")
        assert deleted_legacy.status_code == 200
        assert deleted_legacy.json()["facts"] == []

    facts_root = tmp_path / "users" / "alice" / "agents" / "__default__" / "facts"
    assert facts_root.exists()
    assert not list(facts_root.glob("**/*.md"))
    assert "facts" not in json.loads(memory_path.read_text(encoding="utf-8"))


def test_update_memory_fact_route_preserves_omitted_fields() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    updated_memory = _sample_memory(facts=[{"id": "fact_edit", "content": "User prefers spaces", "category": "preference", "confidence": 0.8, "createdAt": "2026-03-20T00:00:00Z", "source": "manual"}])

    mock_mgr = MagicMock()
    mock_mgr.update_fact.return_value = updated_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.patch("/api/memory/facts/fact_edit", json={"content": "User prefers spaces"})
    assert response.status_code == 200
    # The router calls _require_capability("update_fact") -> getattr(mgr, "update_fact")
    # which returns mock_mgr.update_fact (a MagicMock).  Then the call is
    #   update_fact(fact_id=..., content=..., category=..., confidence=..., user_id=...)
    mock_mgr.update_fact.assert_called_once()
    call_kwargs = mock_mgr.update_fact.call_args.kwargs
    assert call_kwargs.get("fact_id") == "fact_edit"
    assert call_kwargs.get("content") == "User prefers spaces"
    assert call_kwargs.get("category") is None
    assert call_kwargs.get("confidence") is None
    assert "user_id" in call_kwargs
    assert response.json()["facts"] == updated_memory["facts"]


def test_update_memory_fact_route_returns_404_for_missing_fact() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    mock_mgr = MagicMock()
    mock_mgr.update_fact.side_effect = KeyError("fact_missing")
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.patch("/api/memory/facts/fact_missing", json={"content": "User prefers spaces", "category": "workflow", "confidence": 0.91})
    assert response.status_code == 404
    assert response.json()["detail"] == "Memory fact 'fact_missing' not found."


def test_update_memory_fact_route_returns_specific_error_for_invalid_confidence() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    mock_mgr = MagicMock()
    mock_mgr.update_fact.side_effect = ValueError("confidence")
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with TestClient(app) as client:
            response = client.patch("/api/memory/facts/fact_edit", json={"content": "User prefers spaces", "confidence": 0.91})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid confidence value; must be between 0 and 1."


# ── bound-owner (internal caller) ──────────────────────────────────────────


def _internal_owner_request(owner_user_id: str) -> SimpleNamespace:
    from app.gateway.internal_auth import INTERNAL_OWNER_USER_ID_HEADER_NAME, INTERNAL_SYSTEM_ROLE
    from deerflow.runtime.user_context import DEFAULT_USER_ID

    return SimpleNamespace(
        headers={INTERNAL_OWNER_USER_ID_HEADER_NAME: owner_user_id},
        state=SimpleNamespace(user=SimpleNamespace(id=DEFAULT_USER_ID, system_role=INTERNAL_SYSTEM_ROLE)),
    )


def test_get_memory_honors_bound_owner_header() -> None:
    seen: dict[str, str] = {}

    def fake_get_memory(*, user_id: str) -> dict:
        seen["user_id"] = user_id
        return _sample_memory(facts=[{"id": "f", "content": "owner fact", "category": "context", "confidence": 0.9, "createdAt": "", "source": "owner"}])

    mock_mgr = MagicMock()
    mock_mgr.get_memory.side_effect = fake_get_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        response = asyncio.run(call_unwrapped(memory.get_memory, _internal_owner_request("owner-1")))
    assert seen["user_id"] == "owner-1"
    assert response.facts[0].content == "owner fact"


def test_get_memory_sanitizes_unsafe_owner_header() -> None:
    from deerflow.config.paths import make_safe_user_id

    raw_owner = "feishu|ou_AbC/123"
    seen: dict[str, str] = {}

    def fake_get_memory(*, user_id: str) -> dict:
        seen["user_id"] = user_id
        return _sample_memory()

    mock_mgr = MagicMock()
    mock_mgr.get_memory.side_effect = fake_get_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        asyncio.run(call_unwrapped(memory.get_memory, _internal_owner_request(raw_owner)))
    expected = make_safe_user_id(raw_owner)
    assert seen["user_id"] == expected
    assert seen["user_id"] != raw_owner


def test_get_memory_falls_back_to_effective_user_for_browser_requests() -> None:
    from app.gateway.internal_auth import INTERNAL_OWNER_USER_ID_HEADER_NAME

    seen: dict[str, str] = {}

    def fake_get_memory(*, user_id: str) -> dict:
        seen["user_id"] = user_id
        return _sample_memory()

    browser_request = SimpleNamespace(
        headers={INTERNAL_OWNER_USER_ID_HEADER_NAME: "owner-1"},
        state=SimpleNamespace(user=SimpleNamespace(id="real-user", system_role="user")),
    )

    mock_mgr = MagicMock()
    mock_mgr.get_memory.side_effect = fake_get_memory
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        with patch("app.gateway.routers.memory.get_effective_user_id", return_value="real-user"):
            asyncio.run(call_unwrapped(memory.get_memory, browser_request))
    assert seen["user_id"] == "real-user"


def _browser_request_with_spoofed_owner_header() -> SimpleNamespace:
    from app.gateway.internal_auth import INTERNAL_OWNER_USER_ID_HEADER_NAME

    return SimpleNamespace(
        headers={INTERNAL_OWNER_USER_ID_HEADER_NAME: "owner-1"},
        state=SimpleNamespace(user=SimpleNamespace(id="real-user", system_role="user")),
    )


def test_clear_memory_scopes_destructive_write_to_bound_owner() -> None:
    seen: dict[str, str] = {}

    def fake_clear(*, user_id: str) -> dict:
        seen["user_id"] = user_id
        return _sample_memory()

    mock_mgr = MagicMock()
    mock_mgr.clear_memory.side_effect = fake_clear
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        asyncio.run(call_unwrapped(memory.clear_memory, _internal_owner_request("owner-1")))
        assert seen["user_id"] == "owner-1"

        with patch("app.gateway.routers.memory.get_effective_user_id", return_value="real-user"):
            asyncio.run(call_unwrapped(memory.clear_memory, _browser_request_with_spoofed_owner_header()))
        assert seen["user_id"] == "real-user"


def test_import_memory_scopes_overwrite_to_bound_owner() -> None:
    seen: dict[str, str] = {}
    payload = memory.MemoryResponse(**_sample_memory())

    def fake_import(_data: dict, *, user_id: str) -> dict:
        seen["user_id"] = user_id
        return _sample_memory()

    mock_mgr = MagicMock()
    mock_mgr.import_memory.side_effect = fake_import
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=mock_mgr):
        asyncio.run(call_unwrapped(memory.import_memory, payload, _internal_owner_request("owner-1")))
        assert seen["user_id"] == "owner-1"

        with patch("app.gateway.routers.memory.get_effective_user_id", return_value="real-user"):
            asyncio.run(call_unwrapped(memory.import_memory, payload, _browser_request_with_spoofed_owner_header()))
        assert seen["user_id"] == "real-user"


# ── unsupported-backend 501s ────────────────────────────────────────────────
# A minimal backend (only add + get_context) inherits the tier-2/tier-3 default
# raise for get_memory / clear_memory / import_memory / reload_memory. Before
# the contract change these were @abstractmethod (every backend implemented
# them, so the endpoints could never raise); now the endpoints catch
# NotImplementedError -> 501 so an unsupported backend gets a clean "not
# supported" instead of a raw 500 (there is no global NotImplementedError
# handler, so an uncaught raise is a 500).


def _unsupported_manager() -> MagicMock:
    """Mock a minimal backend: read/manage ops raise NotImplementedError."""
    mock_mgr = MagicMock()
    mock_mgr.get_memory.side_effect = NotImplementedError("get_memory not supported")
    mock_mgr.clear_memory.side_effect = NotImplementedError("clear_memory not supported")
    mock_mgr.import_memory.side_effect = NotImplementedError("import_memory not supported")
    mock_mgr.reload_memory.side_effect = NotImplementedError("reload_memory not supported")
    return mock_mgr


def test_get_memory_route_returns_501_for_unsupported_backend() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=_unsupported_manager()):
        with TestClient(app) as client:
            response = client.get("/api/memory")
    assert response.status_code == 501
    assert "not supported" in response.json()["detail"]


def test_export_memory_route_returns_501_for_unsupported_backend() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=_unsupported_manager()):
        with TestClient(app) as client:
            response = client.get("/api/memory/export")
    assert response.status_code == 501


def test_memory_status_route_returns_501_for_unsupported_backend() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    cfg = SimpleNamespace(
        enabled=True,
        mode="middleware",
        injection_enabled=True,
        shutdown_flush_timeout_seconds=30.0,
        manager_class="minimal",
        backend_config={},
    )
    with (
        patch("app.gateway.routers.memory.get_memory_manager", return_value=_unsupported_manager()),
        patch("app.gateway.routers.memory.get_memory_config", return_value=cfg),
    ):
        with TestClient(app) as client:
            response = client.get("/api/memory/status")
    assert response.status_code == 501


def test_clear_memory_route_returns_501_for_unsupported_backend() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=_unsupported_manager()):
        with TestClient(app) as client:
            response = client.delete("/api/memory")
    assert response.status_code == 501


def test_import_memory_route_returns_501_for_unsupported_backend() -> None:
    app = make_authed_test_app()
    app.include_router(memory.router)
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=_unsupported_manager()):
        with TestClient(app) as client:
            response = client.post("/api/memory/import", json=_sample_memory())
    assert response.status_code == 501


def test_reload_memory_route_returns_501_when_read_also_unsupported() -> None:
    """reload falls back to get_memory; if both raise (minimal backend), the
    fallback surfaces 501 instead of a raw 500 from the uncaught raise."""
    app = make_authed_test_app()
    app.include_router(memory.router)
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=_unsupported_manager()):
        with TestClient(app) as client:
            response = client.post("/api/memory/reload")
    assert response.status_code == 501


def test_import_blank_fact_returns_400_without_replacing_saved_memory(tmp_path):
    manager = DeerMem(backend_config={"storage_path": str(tmp_path)})
    before = manager.import_memory(_sample_memory(facts=[{"id": "keep", "content": "Saved preference"}]), user_id="alice")
    payload = _sample_memory(facts=[{**before["facts"][0], "content": "   "}])
    app = make_authed_test_app()
    app.include_router(memory.router)
    with (
        patch("app.gateway.routers.memory.get_memory_manager", return_value=manager),
        patch("app.gateway.routers.memory.get_effective_user_id", return_value="alice"),
        TestClient(app) as client,
    ):
        response = client.post("/api/memory/import", json=payload)
    assert response.status_code == 400
    assert manager.get_memory(user_id="alice") == before


# ── trusted requester (HTTP path) ───────────────────────────────────────────
# The requester is the user a request acts on behalf of. It is NOT the storage
# owner the router resolves for the bucket (`_resolve_memory_user_id`), and it
# is never taken from the request. The only trusted carrier is the task-local
# value the governance middleware publishes once its identity chain resolves
# (`set_requester_id`, gov#63); memory's run-time paths read the same value
# through `requester_from_runtime`. These tests drive the HTTP endpoints with
# the production OpenViking manager (real scope resolution, fake OpenViking
# client) so "the requester reaches the backend on the HTTP path" is asserted
# end-to-end rather than at the manager boundary.

_REFUSED_DETAIL = "Memory operation refused for this requester."
_ALICE_FACT = "viking://~/peers/alice/memories/fact.md"
_BOB_FACT = "viking://~/peers/bob/memories/fact.md"


class _PublishRequester:
    """Publish a trusted requester for the request task.

    Stands in for the governance middleware, whose only role here is to call
    ``set_requester_id`` inside the request task before the endpoint reads it.
    ``None`` models "the identity chain could not be resolved", which must stay
    distinguishable from a resolved id.
    """

    def __init__(self, app: ASGIApp, requester: str | None = None) -> None:
        self.app = app
        self.requester = requester

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = set_requester_id(self.requester)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_requester_id(token)


class _RequesterScopedRecorder:
    """Manager stub that declares requester scope and records every call."""

    supports_requester_scope = True

    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}

    def _record(self, name: str, kwargs: dict[str, Any]) -> dict:
        self.calls[name] = kwargs
        return _sample_memory()

    def get_memory(self, **kwargs: Any) -> dict:
        return self._record("get_memory", kwargs)

    def reload_memory(self, **kwargs: Any) -> dict:
        return self._record("reload_memory", kwargs)

    def clear_memory(self, **kwargs: Any) -> dict:
        return self._record("clear_memory", kwargs)

    def create_fact(self, content: str, category: str, confidence: float, **kwargs: Any) -> tuple[dict, str]:
        return self._record("create_fact", kwargs), "fact_new"

    def delete_fact(self, fact_id: str, **kwargs: Any) -> dict:
        return self._record("delete_fact", kwargs)

    def update_fact(self, fact_id: str, content: str | None, category: str | None, confidence: float | None, **kwargs: Any) -> dict:
        return self._record("update_fact", kwargs)

    def import_memory(self, data: dict, **kwargs: Any) -> dict:
        return self._record("import_memory", kwargs)


def _requester_app(*, requester: str | None, owner: str = "alice") -> FastAPI:
    """An internal caller's app: owner bucket *owner*, requester *requester*."""
    app = make_authed_test_app(user_factory=lambda: SimpleNamespace(id=DEFAULT_USER_ID, system_role=INTERNAL_SYSTEM_ROLE))
    app.add_middleware(_PublishRequester, requester=requester)
    app.include_router(memory.router)
    return app


def _owner_headers(owner: str = "alice") -> dict[str, str]:
    return {INTERNAL_OWNER_USER_ID_HEADER_NAME: owner}


def _fact_ids_over_http(manager: object, *, requester: str | None, path: str = "/api/memory") -> list[str]:
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=manager):
        with TestClient(_requester_app(requester=requester)) as client:
            response = client.get(path, headers=_owner_headers())
    assert response.status_code == 200, response.text
    return [fact["id"] for fact in response.json()["facts"]]


@pytest.fixture
def requester_scoped_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, official_integration: None) -> OpenVikingMemoryManager:
    """The production OpenViking manager in requester scope, over a fake client."""
    manager = _openviking_manager(tmp_path, monkeypatch, memory_scope="requester")
    manager._client.files = {_ALICE_FACT: "alice prefers concise reviews", _BOB_FACT: "bob prefers verbose reviews"}
    return manager


def test_requester_scoped_reads_partition_by_requester_not_owner(requester_scoped_manager: OpenVikingMemoryManager) -> None:
    """Two requesters on one owner bucket see only their own memory."""
    assert _fact_ids_over_http(requester_scoped_manager, requester="alice") == [_ALICE_FACT]
    assert _fact_ids_over_http(requester_scoped_manager, requester="bob") == [_BOB_FACT]
    assert _fact_ids_over_http(requester_scoped_manager, requester="alice", path="/api/memory/export") == [_ALICE_FACT]


def test_requester_scoped_clear_and_delete_stay_inside_the_requesters_bucket(requester_scoped_manager: OpenVikingMemoryManager) -> None:
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=requester_scoped_manager):
        with TestClient(_requester_app(requester="alice")) as client:
            foreign = client.delete("/api/memory/facts/" + quote(_BOB_FACT, safe=""), headers=_owner_headers())
            own = client.delete("/api/memory/facts/" + quote(_ALICE_FACT, safe=""), headers=_owner_headers())
            cleared = client.delete("/api/memory", headers=_owner_headers())
    assert foreign.status_code == 403
    assert foreign.json()["detail"] == _REFUSED_DETAIL
    assert own.status_code == 200
    assert cleared.status_code == 200
    # Bob's file was never touched; alice's own root is the only thing cleared.
    assert requester_scoped_manager._client.removals == [(_ALICE_FACT, False), ("viking://~/peers/alice/memories", True)]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/memory"),
        ("get", "/api/memory/export"),
        ("get", "/api/memory/status"),
        ("post", "/api/memory/reload"),
        ("delete", "/api/memory"),
        ("delete", "/api/memory/facts/" + quote(_ALICE_FACT, safe="")),
    ],
)
def test_requester_scoped_endpoints_refuse_without_a_trusted_requester(requester_scoped_manager: OpenVikingMemoryManager, method: str, path: str) -> None:
    """Requester-scoped memory fails closed instead of serving the owner bucket."""
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=requester_scoped_manager):
        with TestClient(_requester_app(requester=None)) as client:
            response = client.request(method, path, headers=_owner_headers())
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == _REFUSED_DETAIL
    # Refused before the store was touched at all.
    assert requester_scoped_manager._client.reads == []
    assert requester_scoped_manager._client.removals == []


def test_instance_scoped_memory_is_unchanged_without_a_requester(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, official_integration: None) -> None:
    """Instance scope ignores the requester and still serves the owner bucket."""
    manager = _openviking_manager(tmp_path, monkeypatch, memory_scope="instance")
    manager._client.files = {"viking://~/memories/summary.md": "alice summary", "viking://~/peers/research/memories/fact.md": "research fact"}
    assert _fact_ids_over_http(manager, requester=None) == ["viking://~/memories/summary.md", "viking://~/peers/research/memories/fact.md"]


def test_owner_and_request_supplied_ids_are_never_the_requester(requester_scoped_manager: OpenVikingMemoryManager) -> None:
    """The owner id and any request-supplied id must not stand in for it."""
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=requester_scoped_manager):
        with TestClient(_requester_app(requester=None)) as client:
            response = client.get(
                "/api/memory",
                params={"requester_id": "alice"},
                headers={**_owner_headers("alice"), "X-DeerFlow-Requester-Id": "alice"},
            )
    assert response.status_code == 403
    assert requester_scoped_manager._client.reads == []


def test_trusted_requester_reaches_every_memory_operation() -> None:
    """Each memory operation carries the same owner + trusted requester pair."""
    recorder = _RequesterScopedRecorder()
    requests = [
        ("get", "/api/memory", {}),
        ("get", "/api/memory/export", {}),
        ("get", "/api/memory/status", {}),
        ("post", "/api/memory/reload", {}),
        ("delete", "/api/memory", {}),
        ("post", "/api/memory/facts", {"json": {"content": "fact"}}),
        ("delete", "/api/memory/facts/fact", {}),
        ("patch", "/api/memory/facts/fact", {"json": {"content": "fact"}}),
        ("post", "/api/memory/import", {"json": _sample_memory()}),
    ]
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=recorder):
        with TestClient(_requester_app(requester="alice")) as client:
            for method, path, kwargs in requests:
                response = client.request(method, path, headers=_owner_headers(), **kwargs)
                assert response.status_code == 200, f"{method} {path}: {response.text}"
    assert set(recorder.calls) == {"get_memory", "reload_memory", "clear_memory", "create_fact", "delete_fact", "update_fact", "import_memory"}
    assert all(kwargs == {"user_id": "alice", "requester_id": "alice"} for kwargs in recorder.calls.values()), recorder.calls


def test_backends_without_requester_scope_keep_their_call_contract() -> None:
    """A single-scope backend is called exactly as before (no requester kwarg)."""
    manager = MagicMock()
    manager.get_memory.return_value = _sample_memory()
    with patch("app.gateway.routers.memory.get_memory_manager", return_value=manager):
        with TestClient(_requester_app(requester="alice")) as client:
            response = client.get("/api/memory", headers=_owner_headers())
    assert response.status_code == 200
    assert manager.get_memory.call_args.kwargs == {"user_id": "alice"}


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (MemoryConflictError("stale write"), 409),
        (MemoryCorruptionError("corrupt payload"), 500),
        (MemoryReadError("backend read failed"), 502),
        (MemoryManagerError("refused"), 403),
        (RequesterScopeError("requires an authenticated requester"), 403),
    ],
)
@pytest.mark.parametrize(
    ("method", "path", "manager_method", "kwargs"),
    [
        ("get", "/api/memory", "get_memory", {}),
        ("get", "/api/memory/export", "get_memory", {}),
        ("get", "/api/memory/status", "get_memory", {}),
        ("delete", "/api/memory", "clear_memory", {}),
        ("delete", "/api/memory/facts/fact", "delete_fact", {}),
        ("post", "/api/memory/facts", "create_fact", {"json": {"content": "fact"}}),
        ("patch", "/api/memory/facts/fact", "update_fact", {"json": {"content": "fact"}}),
        ("post", "/api/memory/import", "import_memory", {"json": _sample_memory()}),
    ],
)
def test_memory_manager_error_family_maps_to_stable_statuses(method: str, path: str, manager_method: str, kwargs: dict, error: Exception, status: int) -> None:
    """The manager error family has one deterministic HTTP status per case."""
    expected_details = {
        409: "Memory changed concurrently; reload and retry.",
        500: "Stored memory data is corrupted.",
        502: "Memory backend read failed.",
        403: _REFUSED_DETAIL,
    }
    manager = MagicMock()
    getattr(manager, manager_method).side_effect = error
    cfg = SimpleNamespace(enabled=True, mode="middleware", injection_enabled=True, shutdown_flush_timeout_seconds=30.0, manager_class="stub", backend_config={})
    with (
        patch("app.gateway.routers.memory.get_memory_manager", return_value=manager),
        patch("app.gateway.routers.memory.get_memory_config", return_value=cfg),
    ):
        with TestClient(_requester_app(requester="alice")) as client:
            response = client.request(method, path, headers=_owner_headers(), **kwargs)
    assert response.status_code == status, response.text
    assert response.json()["detail"] == expected_details[status]
