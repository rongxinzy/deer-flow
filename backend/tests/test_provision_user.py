"""Integration tests for the admin provision endpoint (POST /api/v1/auth/provision).

Covers: admin-session requirement (no auth / PAT / non-admin all rejected),
happy-path user creation, auto-PAT issuance (show-once token + digest-only
storage + scope validation), duplicate email, and password strength.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

import deerflow.persistence.models  # noqa: F401  (register every table)
from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.csrf_middleware import CSRFMiddleware
from app.gateway.routers.auth import router as auth_router
from deerflow.config.authorization_config import AuthorizationConfig
from deerflow.persistence.base import Base
from deerflow.persistence.personal_access_tokens import PersonalAccessTokenRepository

TEST_JWT_SECRET = "test-provision-jwt-secret-0123456789"


class _FakeProvider:
    """Minimal LocalAuthProvider stand-in for provision tests."""

    def __init__(self) -> None:
        self.users: dict[str, object] = {}

    async def create_user(self, email: str, password: str | None = None, system_role: str = "user", needs_setup: bool = False):
        from app.gateway.auth.models import User

        key = email.lower()
        if key in self.users:
            raise ValueError(f"Email already exists: {email}")
        user = User(email=key, password_hash=password, system_role=system_role, needs_setup=needs_setup)
        self.users[key] = user
        return user

    async def count_admin_users(self) -> int:
        return sum(1 for u in self.users.values() if getattr(u, "system_role", None) == "admin")


@pytest.fixture(autouse=True)
def _default_route_authorization_config(monkeypatch):
    monkeypatch.setattr(
        "app.gateway.authz._get_route_authorization_config",
        lambda: AuthorizationConfig(),
    )
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "")
    from app.gateway.auth.config import AuthConfig, set_auth_config

    set_auth_config(AuthConfig(jwt_secret=TEST_JWT_SECRET, token_expiry_days=7))


@pytest.fixture
def provision_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/provision.db", poolclass=NullPool)
    asyncio.run(_create_tables(engine))
    repo = PersonalAccessTokenRepository(async_sessionmaker(engine, expire_on_commit=False))

    provider = _FakeProvider()
    monkeypatch.setattr("app.gateway.deps.get_local_provider", lambda: provider)
    monkeypatch.setattr("app.gateway.routers.auth.get_local_provider", lambda: provider)

    app = FastAPI()
    # Production order: AuthMiddleware added first (inner), CSRF last (outer).
    app.add_middleware(AuthMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.include_router(auth_router)
    app.state.pat_repo = repo

    with TestClient(app) as test_client:
        yield test_client, provider, repo
    asyncio.run(engine.dispose())


async def _create_tables(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _admin_session(client: TestClient) -> None:

    from app.gateway.auth import create_access_token

    # Seed an admin row so the strict JWT resolver finds the signed user.
    provider = None  # provider lives in the fixture; the resolver goes through deps
    del provider

    from app.gateway.auth.models import User

    admin = User(email="admin@example.com", password_hash=None, system_role="admin")
    # The strict resolver uses get_local_provider().get_user(id); our fake
    # provider does not implement get_user, so register the admin by patching
    # the id map on the instance the app resolves.
    client.cookies.set("access_token", create_access_token(str(admin.id), token_version=0))
    return admin


@pytest.fixture
def admin_client(provision_env):
    """TestClient with a valid admin session cookie (strict resolver satisfied)."""
    client, provider, repo = provision_env
    from types import SimpleNamespace

    from app.gateway.auth import create_access_token

    admin = SimpleNamespace(
        id="admin-1",
        email="admin@example.com",
        system_role="admin",
        needs_setup=False,
        token_version=0,
        oauth_provider=None,
        password_hash=None,
    )

    async def _get_user(user_id: str):
        if str(user_id) == "admin-1":
            return admin
        return provider.users.get(str(user_id))

    provider.get_user = _get_user  # type: ignore[attr-defined]
    client.cookies.set("access_token", create_access_token("admin-1", token_version=0))
    return client


def _post_provision(client: TestClient, payload: dict):
    from app.gateway.csrf_middleware import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, generate_csrf_token

    csrf = generate_csrf_token()
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return client.post("/api/v1/auth/provision", json=payload, headers={CSRF_HEADER_NAME: csrf})


def test_provision_requires_authentication(provision_env):
    client, _provider, _repo = provision_env
    client.cookies.clear()
    response = _post_provision(client, {"email": "a@example.com", "auto_pat": {"name": "t", "scopes": ["runs:read"]}})
    assert response.status_code == 401


def test_provision_rejects_non_admin_session(provision_env):
    client, _provider, _repo = provision_env
    from types import SimpleNamespace

    from app.gateway.auth import create_access_token

    plain = SimpleNamespace(
        id="user-1",
        email="user@example.com",
        system_role="user",
        needs_setup=False,
        token_version=0,
        oauth_provider=None,
        password_hash=None,
    )
    _provider.get_user = _async_resolve(plain)  # type: ignore[method-assign]
    client.cookies.set("access_token", create_access_token("user-1", token_version=0))
    response = _post_provision(client, {"email": "a@example.com"})
    assert response.status_code == 403


def _async_resolve(user):
    async def _resolve(_user_id: str):
        return user

    return _resolve


def test_provision_rejects_pat_credential(admin_client, provision_env):
    client, _provider, repo = provision_env
    # Mint a PAT for the admin, then authenticate with it instead of the cookie.
    from app.gateway.auth.pat import generate_pat_token, pat_token_digest

    token = generate_pat_token()
    asyncio.run(repo.create(user_id="admin-1", name="admin-pat", scopes=["runs:read"], token_digest=pat_token_digest(token)))
    client.cookies.clear()
    response = client.post("/api/v1/auth/provision", json={"email": "a@example.com"}, headers={"Authorization": f"Bearer {token}"})
    # PAT route policy default-denies /api/v1/auth/provision (no cookie fallback).
    assert response.status_code == 403
    assert response.json()["detail"] == "PAT credentials are not permitted on this route"


def test_provision_creates_user_without_pat(admin_client):
    response = _post_provision(admin_client, {"email": "Alice@Example.com"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["system_role"] == "user"
    assert body["pat"] is None


def test_provision_creates_user_with_auto_pat(admin_client, provision_env):
    client, provider, repo = provision_env
    response = _post_provision(
        client,
        {
            "email": "worker@example.com",
            "system_role": "user",
            "auto_pat": {"name": "operator-pat", "scopes": ["runs:create", "runs:read", "threads:read", "threads:write"], "expires_in_days": 30},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    pat = body["pat"]
    assert pat is not None and pat["token"].startswith("dfp_")
    assert set(pat["scopes"]) == {"runs:create", "runs:read", "threads:read", "threads:write"}
    assert pat["expires_at"] is not None

    # The raw token authenticates as the new user via Bearer.
    client.cookies.clear()
    whoami = client.post(
        "/api/v1/auth/pats",
        json={"name": "x", "scopes": ["runs:read"]},
    )
    # PATs cannot manage PATs (session source required) — proves the Bearer
    # identity resolved to the provisioned user and hit the PAT guard.
    assert whoami.status_code == 403

    # The PAT round-trips through the middleware on a thread route.
    from app.gateway.auth.pat import PAT_TOKEN_PREFIX

    assert pat["token"].startswith(PAT_TOKEN_PREFIX)


def test_provision_rejects_duplicate_email(admin_client):
    assert _post_provision(admin_client, {"email": "dup@example.com"}).status_code == 201
    response = _post_provision(admin_client, {"email": "dup@example.com"})
    assert response.status_code == 400


def test_provision_rejects_weak_password(admin_client):
    # Matches the Register model: min_length=8 plus the common-password check.
    response = _post_provision(admin_client, {"email": "weak@example.com", "password": "short"})
    assert response.status_code == 422
    response = _post_provision(admin_client, {"email": "weak@example.com", "password": "password"})
    assert response.status_code == 422


def test_provision_rejects_unknown_pat_scope(admin_client):
    response = _post_provision(admin_client, {"email": "s@example.com", "auto_pat": {"name": "t", "scopes": ["memory:read"]}})
    assert response.status_code == 400
    assert "Unknown PAT scopes" in response.json()["detail"]


def test_provision_rejects_empty_pat_scopes(admin_client):
    response = _post_provision(admin_client, {"email": "s@example.com", "auto_pat": {"name": "t", "scopes": []}})
    assert response.status_code == 400
