"""Enterprise channel capability never elevates normal internal chat callers."""

import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.gateway import channel_admin, internal_auth


def channel_request(*, role="internal", owner="admin-test", method="POST", path="/api/channels/feishu/runtime-config", signed=True):
    message = f"zhiyuan-channel-admin-v1\n{method}\n{path}\n{owner}"
    headers = [(b"x-deerflow-owner-user-id", owner.encode())]
    if signed:
        signature = hmac.new(internal_auth._INTERNAL_AUTH_TOKEN.encode(), message.encode(), hashlib.sha256).hexdigest()
        headers.append((b"x-zhiyuan-channel-admin", signature.encode()))
    request = Request({"type": "http", "method": method, "path": path, "headers": headers, "query_string": b""})
    request.state.user = SimpleNamespace(id=owner, system_role=role)
    return request


@pytest.mark.parametrize(
    "role,owner,path,signed,expected",
    [
        ("internal", "admin-test", "/api/channels/feishu/runtime-config", True, True),
        ("internal", "admin-test", "/api/channels/wechat/qr-login", True, True),
        ("internal", "admin-test", "/api/mcp/config", True, False),
        ("user", "admin-test", "/api/channels/feishu/runtime-config", True, False),
        ("internal", "", "/api/channels/feishu/runtime-config", True, False),
        ("internal", "admin-test", "/api/channels/feishu/runtime-config", False, False),
    ],
)
def test_capability_requires_internal_identity_and_channel_scope(role, owner, path, signed, expected):
    request = channel_request(role=role, owner=owner, path=path, signed=signed)
    assert channel_admin.is_channel_admin_request(request) is expected


@pytest.mark.parametrize("changed", ["method", "path", "owner", "signature"])
def test_capability_rejects_replayed_or_modified_request(changed):
    request = channel_request()
    if changed == "method":
        request.scope["method"] = "DELETE"
    elif changed == "path":
        request.scope["path"] = "/api/channels/wecom/runtime-config"
    else:
        key = b"x-deerflow-owner-user-id" if changed == "owner" else b"x-zhiyuan-channel-admin"
        request.scope["headers"] = [(k, b"other" if k == key else v) for k, v in request.scope["headers"]]
    assert not channel_admin.is_channel_admin_request(request)


@pytest.mark.asyncio
async def test_regular_internal_request_cannot_configure_channel(monkeypatch):
    from app.gateway.routers import channel_connections

    configure = AsyncMock()
    monkeypatch.setattr(channel_connections, "_configure_channel_provider_runtime", configure)
    with pytest.raises(HTTPException) as denied:
        await channel_connections.configure_channel_provider_runtime("feishu", channel_connections.ChannelRuntimeConfigRequest(values={"app_id": "test", "app_secret": "test"}), channel_request(signed=False))
    assert denied.value.status_code == 403
    configure.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_control_service_reaches_existing_runtime_handler(monkeypatch):
    from app.gateway.routers import channel_connections

    expected = {"provider": "feishu"}
    configure = AsyncMock(return_value=expected)
    monkeypatch.setattr(channel_connections, "_configure_channel_provider_runtime", configure)
    result = await channel_connections.configure_channel_provider_runtime("feishu", channel_connections.ChannelRuntimeConfigRequest(values={"app_id": "test", "app_secret": "test"}), channel_request())
    assert result == expected
    configure.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_browser_admin_keeps_access():
    await channel_admin.require_channel_admin(channel_request(role="admin", signed=False), detail="denied")


def test_real_http_auth_middleware_requires_the_signed_channel_capability(monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from app.gateway.auth_middleware import AuthMiddleware
    from app.gateway.routers import channel_connections
    from deerflow.config.authorization_config import AuthorizationConfig

    monkeypatch.setattr("app.gateway.authz._get_route_authorization_config", lambda: AuthorizationConfig())
    configure = AsyncMock(
        return_value={
            "provider": "feishu",
            "display_name": "Feishu",
            "enabled": True,
            "configured": True,
            "connectable": True,
            "auth_mode": "binding_code",
            "connection_status": "not_connected",
        }
    )
    monkeypatch.setattr(channel_connections, "_configure_channel_provider_runtime", configure)
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(channel_connections.router)
    request = channel_request()
    headers = dict(request.headers)
    headers["X-DeerFlow-Internal-Token"] = internal_auth._INTERNAL_AUTH_TOKEN
    with TestClient(app) as client:
        allowed = client.post("/api/channels/feishu/runtime-config", headers=headers, json={"values": {"app_id": "test", "app_secret": "test"}})
        assert allowed.status_code == 200
        configure.assert_awaited_once()
        headers.pop("x-zhiyuan-channel-admin")
        rejected = client.post("/api/channels/feishu/runtime-config", headers=headers, json={"values": {"app_id": "test", "app_secret": "test"}})
        assert rejected.status_code == 403
        configure.assert_awaited_once()
