"""WeChat iLink request metadata encoding."""

import base64
import secrets


def build_ilink_client_version(version: str) -> str:
    parts = [part.strip() for part in version.split(".")]

    def _part(index: int) -> int:
        if index >= len(parts):
            return 0
        try:
            return max(0, min(int(parts[index] or 0), 0xFF))
        except ValueError:
            return 0

    major = _part(0)
    minor = _part(1)
    patch = _part(2)
    return str((major << 16) | (minor << 8) | patch)


def build_wechat_uin() -> str:
    return base64.b64encode(str(secrets.randbits(32)).encode("utf-8")).decode("utf-8")
