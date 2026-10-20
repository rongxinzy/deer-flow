"""Opt-in OpenAI-compatible models authenticated by a rotating token file."""

from __future__ import annotations

import os
import re
import stat
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from deerflow.models.patched_openai import PatchedChatOpenAI

_MAX_TOKEN_BYTES = 65536
_BEARER = re.compile(rb"[A-Za-z0-9._~+/-]+=*")


class TokenFileError(ValueError):
    """The workload credential cannot be used; no fallback is permitted."""


def _read_token(path: Path) -> str:
    # Open the projected name anew, not its resolved target: Kubernetes replaces
    # the ..data symlink atomically when rotating projected ServiceAccount tokens.
    # NONBLOCK lets a misconfigured FIFO fail instead of hanging an SDK worker.
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            raw = stream.read(_MAX_TOKEN_BYTES + 1)
        token = raw.rstrip(b"\r\n")
        if len(raw) > _MAX_TOKEN_BYTES or not _BEARER.fullmatch(token):
            raise ValueError
        return token.decode("ascii")
    except (OSError, ValueError):
        # Neither filesystem paths, bearer contents nor chained OS errors should
        # reach provider errors/traces. Never reuse an earlier successful token.
        raise TokenFileError("Model token file is unavailable or invalid.") from None


def _check_headers(headers: Any) -> None:
    if headers and any(str(key).lower() == "authorization" for key in headers):
        raise ValueError("Authorization headers cannot override the model token file.")


def _token_file_values(values: Any, endpoint_keys: tuple[str, ...]) -> dict[str, Any]:
    values = dict(values)
    raw_path = values.get("api_key_file")
    if not isinstance(raw_path, (str, Path)) or not Path(raw_path).is_absolute():
        raise ValueError("api_key_file must be an absolute path.")
    for key in ("api_key", "openai_api_key", "client", "async_client", "root_client", "root_async_client"):
        if values.get(key) is not None:
            raise ValueError("Static credentials and preconstructed SDK clients cannot override the model token file.")
    # Require the destination explicitly; environment defaults must not send a
    # workload bearer token to a public provider or a different gateway.
    endpoints = [values[key] for key in endpoint_keys if values.get(key) is not None]
    if not endpoints or any(not isinstance(value, str) or value != endpoints[0] for value in endpoints):
        raise ValueError("One explicit, non-conflicting trusted gateway endpoint is required.")
    parsed = urlsplit(endpoints[0])
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("The gateway endpoint must be HTTP(S) without credentials, query or fragment.")
    _check_headers(values.get("default_headers"))
    _check_headers((values.get("model_kwargs") or {}).get("extra_headers"))
    values["api_key_file"] = Path(raw_path)
    return values


class TokenFileChatOpenAI(PatchedChatOpenAI):
    """Read a trusted workload bearer file for each native SDK request.

    The SDK supports a synchronous API-key callable and offloads it for async
    requests. No custom HTTP transport, token cache or renewal loop is needed.
    This adapter does not confer model entitlement or issue workload credentials.
    """

    api_key_file: Path = Field(exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def configure_token_file(cls, values: Any) -> Any:
        values = _token_file_values(values, ("base_url", "openai_api_base"))
        values["api_key"] = partial(_read_token, values["api_key_file"])
        return values

    @classmethod
    def is_lc_serializable(cls) -> bool:
        # A callable closes over the file name; do not persist it in LangChain
        # serialized constructors or traces. Rebuild from trusted operator config.
        return False

    def _get_request_payload(self, *args: Any, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(*args, **kwargs)
        _check_headers(payload.get("extra_headers"))
        return payload
