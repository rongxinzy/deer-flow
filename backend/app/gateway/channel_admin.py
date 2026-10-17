"""A channel-only capability for an enterprise control service.

Ordinary internal chat callers remain non-admin. The control service must sign
the exact method, path and human identity with the runtime's internal token;
the capability cannot authorize any other Gateway administration surface.
"""

import hashlib
import hmac

from fastapi import Request

from app.gateway import internal_auth
from app.gateway.deps import require_admin_user
from app.gateway.internal_auth import (
    INTERNAL_OWNER_USER_ID_HEADER_NAME,
    INTERNAL_SYSTEM_ROLE,
)

CHANNEL_ADMIN_HEADER = "X-Zhiyuan-Channel-Admin"


def is_channel_admin_request(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    if getattr(user, "system_role", None) != INTERNAL_SYSTEM_ROLE:
        return False
    path = request.url.path
    if not path.startswith("/api/channels/"):
        return False
    owner = request.headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME, "")
    signature = request.headers.get(CHANNEL_ADMIN_HEADER, "")
    if not owner or not signature:
        return False
    message = f"zhiyuan-channel-admin-v1\n{request.method}\n{path}\n{owner}"
    expected = hmac.new(internal_auth._INTERNAL_AUTH_TOKEN.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


async def require_channel_admin(request: Request, *, detail: str) -> None:
    if not is_channel_admin_request(request):
        await require_admin_user(request, detail=detail)
