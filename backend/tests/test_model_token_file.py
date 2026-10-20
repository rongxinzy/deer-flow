"""Exercise native LangChain/OpenAI credentials at the actual HTTP boundary."""

import json
import os
from pathlib import Path

import httpx
import pytest

from deerflow.config.app_config import AppConfig
from deerflow.models.factory import create_chat_model
from deerflow.models.token_file import TokenFileChatOpenAI, TokenFileError


def response(request):
    body = json.loads(request.content)
    assert "api_key_file" not in body
    if body.get("stream"):
        chunk = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "gateway-model", "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
    return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1, "model": "gateway-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})


def config(path, **overrides):
    return {"model": "gateway-model", "api_key_file": str(path), "base_url": "https://gateway.invalid/v1", "max_retries": 0, **overrides}


def test_same_model_rereads_atomically_replaced_file_for_sync_calls_and_streams(tmp_path):
    path = tmp_path / "token"
    path.write_text("fixture-first", encoding="utf-8")
    seen = []

    def send(request):
        seen.append(request.headers["authorization"])
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        model = TokenFileChatOpenAI(**config(path, http_client=client))
        assert model.invoke("hello").content == "OK"
        replacement = tmp_path / "replacement"
        replacement.write_text("fixture-second\n", encoding="utf-8")
        replacement.replace(path)
        assert "".join(chunk.content for chunk in model.stream("again")) == "OK"
        assert seen == ["Bearer fixture-first", "Bearer fixture-second"]
        assert "fixture-first" not in repr(model)
        assert "fixture-second" not in repr(model.to_json())
        assert "api_key_file" not in model.model_dump()


@pytest.mark.asyncio
async def test_same_model_rereads_file_for_async_calls_and_streams(tmp_path):
    path = tmp_path / "token"
    path.write_text("fixture-first", encoding="utf-8")
    seen = []

    def send(request):
        seen.append(request.headers["authorization"])
        return response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        model = TokenFileChatOpenAI(**config(path, http_async_client=client))
        assert (await model.ainvoke("hello")).content == "OK"
        path.write_text("fixture-second", encoding="utf-8")
        assert "".join([chunk.content async for chunk in model.astream("again")]) == "OK"
        assert seen == ["Bearer fixture-first", "Bearer fixture-second"]


@pytest.mark.parametrize("value", [b"", b"  \n", b"bad\r\nInjected: value", b"bad secret", b"\xff", b"x" * 65537])
def test_invalid_file_fails_before_http_without_fallback_or_secret_error(tmp_path, monkeypatch, value):
    path = tmp_path / "token"
    path.write_bytes(value)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-must-not-be-used")
    seen = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: seen.append(request))) as client:
        model = TokenFileChatOpenAI(**config(path, http_client=client))
        with pytest.raises(TokenFileError) as error:
            model.invoke("hello")
    assert seen == []
    assert str(error.value) == "Model token file is unavailable or invalid."


def test_deleting_file_after_a_success_does_not_reuse_cached_token(tmp_path):
    path = tmp_path / "token"
    path.write_text("fixture-first", encoding="utf-8")
    seen = []

    def send(request):
        seen.append(True)
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        model = TokenFileChatOpenAI(**config(path, http_client=client))
        model.invoke("hello")
        path.unlink()
        with pytest.raises(TokenFileError):
            model.invoke("again")
    assert seen == [True]


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": "fixture-static"},
        {"openai_api_key": "fixture-static"},
        {"default_headers": {"AUTHORIZATION": "fixture-static"}},
        {"client": object()},
        {"root_async_client": object()},
        {"base_url": None},
        {"base_url": "file:///tmp/model"},
        {"base_url": "https://user:password@gateway.invalid/v1"},
        {"api_key_file": "relative-token"},
    ],
)
def test_rejects_conflicting_credentials_clients_or_implicit_target(tmp_path, overrides):
    with pytest.raises(ValueError):
        TokenFileChatOpenAI(**config(tmp_path / "not-read", **overrides))


def test_factory_can_build_opt_in_profile_without_changing_existing_adapter(tmp_path):
    profile = {"name": "workload", "use": "deerflow.models.token_file:TokenFileChatOpenAI", **config(tmp_path / "not-read")}
    app = AppConfig.model_validate({"sandbox": {"use": "test"}, "models": [profile]})
    model = create_chat_model("workload", app_config=app, attach_tracing=False)
    assert isinstance(model, TokenFileChatOpenAI)
    assert isinstance(model.api_key_file, Path)


@pytest.mark.skipif(os.name != "posix", reason="Kubernetes projected volume uses POSIX symlinks")
def test_projected_volume_symlink_rotation_is_not_pinned(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "token").write_text("fixture-first")
    (second / "token").write_text("fixture-second")
    current = tmp_path / "..data"
    current.symlink_to(first, target_is_directory=True)
    path = tmp_path / "token"
    path.symlink_to("..data/token")
    seen = []

    def send(request):
        seen.append(request.headers["authorization"])
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        model = TokenFileChatOpenAI(**config(path, http_client=client))
        model.invoke("first")
        replacement = tmp_path / "..new"
        replacement.symlink_to(second, target_is_directory=True)
        replacement.replace(current)
        model.invoke("second")
    assert seen == ["Bearer fixture-first", "Bearer fixture-second"]


def test_request_headers_cannot_override_file_credential(tmp_path):
    path = tmp_path / "token"
    path.write_text("fixture-first")
    seen = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: seen.append(request))) as client:
        model = TokenFileChatOpenAI(**config(path, http_client=client))
        with pytest.raises(ValueError, match="Authorization"):
            model.invoke("hello", extra_headers={"aUtHoRiZaTiOn": "fixture-override"})
    assert seen == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO")
def test_non_regular_token_file_fails_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))) as client:
        model = TokenFileChatOpenAI(**config(path, http_client=client))
        with pytest.raises(TokenFileError):
            model.invoke("hello")


def test_native_retry_rereads_credential_after_rotation(tmp_path):
    path = tmp_path / "token"
    path.write_text("fixture-first")
    seen = []

    def send(request):
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            path.write_text("fixture-second")
            return httpx.Response(500, json={"error": {"message": "retry fixture"}})
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        model = TokenFileChatOpenAI(**config(path, http_client=client, max_retries=1))
        assert model.invoke("hello").content == "OK"
    assert seen == ["Bearer fixture-first", "Bearer fixture-second"]


@pytest.mark.asyncio
async def test_async_missing_file_never_falls_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-must-not-be-used")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))) as client:
        model = TokenFileChatOpenAI(**config(tmp_path / "missing", http_async_client=client))
        with pytest.raises(TokenFileError, match="^Model token file is unavailable or invalid\\.$"):
            await model.ainvoke("hello")


@pytest.mark.parametrize("overrides", [{"api_key_file": None}, {"openai_api_base": "https://different.invalid/v1"}, {"base_url": "https://gateway.invalid/v1?api_key=fixture"}])
def test_invalid_path_or_endpoint_alias_cannot_change_credential_target(tmp_path, overrides):
    with pytest.raises(ValueError):
        TokenFileChatOpenAI(**config(tmp_path / "missing", **overrides))
