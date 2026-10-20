"""Native DeepSeek protocol with an opt-in rotating workload credential."""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any, Self

import openai
from pydantic import Field, model_validator

from deerflow.models.patched_deepseek import PatchedChatDeepSeek
from deerflow.models.token_file import _check_headers, _read_token, _token_file_values


async def _read_async_token(path: Path) -> str:
    # The native async OpenAI client expects an awaitable credential provider.
    return await asyncio.to_thread(_read_token, path)


class TokenFileChatDeepSeek(PatchedChatDeepSeek):
    """Preserve DeepSeek semantics while authenticating to a trusted gateway.

    LangChain's DeepSeek constructor currently only accepts a static SecretStr.
    Override its SDK construction to use OpenAI's native key-callable support;
    retain DeepSeek's request/response, reasoning and tool-history adapters.
    """

    api_key_file: Path = Field(exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def configure_token_file(cls, values: Any) -> Any:
        values = _token_file_values(values, ("api_base",))
        if values.get("base_url") is not None or values.get("openai_api_base") is not None:
            raise ValueError("Use the explicit api_base field for the DeepSeek gateway endpoint.")
        # Suppress both inherited environment credential fields, even if set.
        values["api_key"] = None
        values["openai_api_key"] = None
        return values

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        params = {
            "base_url": self.api_base,
            "timeout": self.request_timeout,
            "max_retries": self.max_retries,
            "default_headers": self.default_headers,
            "default_query": self.default_query,
        }
        self.root_client = openai.OpenAI(api_key=partial(_read_token, self.api_key_file), http_client=self.http_client, **params)
        self.client = self.root_client.chat.completions
        self.root_async_client = openai.AsyncOpenAI(api_key=partial(_read_async_token, self.api_key_file), http_client=self.http_async_client, **params)
        self.async_client = self.root_async_client.chat.completions
        return self

    @classmethod
    def is_lc_serializable(cls) -> bool:
        return False

    def _get_request_payload(self, *args: Any, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(*args, **kwargs)
        _check_headers(payload.get("extra_headers"))
        return payload
