"""Rotating workload identity must preserve native DeepSeek tool reasoning."""

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from deerflow.config.app_config import AppConfig
from deerflow.models.factory import create_chat_model
from deerflow.models.token_file import TokenFileError
from deerflow.models.token_file_deepseek import TokenFileChatDeepSeek


def config(path, **overrides):
    return {"model": "aep-flash", "api_key_file": path, "api_base": "http://gateway.invalid/v1", "max_retries": 0, "max_tokens": 128, "extra_body": {"thinking": {"type": "enabled"}}, **overrides}


def send_reply(request, seen):
    body = json.loads(request.content)
    seen.append((request.headers["authorization"], body))
    assert body["model"] == "aep-flash"
    assert body["max_tokens"] == 128
    assert "max_completion_tokens" not in body
    assert "api_key_file" not in body
    assert body["thinking"] == {"type": "enabled"}
    tool_turn = body["messages"][-1]["role"] == "tool"
    if tool_turn:
        assistant = body["messages"][-2]
        assert assistant["reasoning_content"] == "fixture-reasoning"
        assert assistant["tool_calls"][0]["id"] == "fixture-call"
    message = {"content": "OK"} if tool_turn else {"reasoning_content": "fixture-reasoning", "tool_calls": [{"id": "fixture-call", "type": "function", "function": {"name": "check", "arguments": "{}"}}]}
    if body.get("stream"):
        if not tool_turn:
            message["tool_calls"][0]["index"] = 0
        data = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "aep-flash", "choices": [{"index": 0, "delta": message, "finish_reason": None}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n".encode())
    return httpx.Response(
        200, json={"id": "test", "object": "chat.completion", "created": 1, "model": "aep-flash", "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": "stop" if tool_turn else "tool_calls"}]}
    )


def test_sync_rotation_preserves_deepseek_tool_reasoning(tmp_path):
    path = tmp_path / "token"
    path.write_text("fixture-first")
    seen = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: send_reply(request, seen))) as client:
        model = TokenFileChatDeepSeek(**config(path, http_client=client))
        history = [HumanMessage(content="check")]
        first = model.invoke(history)
        assert first.tool_calls[0]["name"] == "check"
        path.write_text("fixture-second")
        history.extend([first, ToolMessage(content="healthy", tool_call_id="fixture-call")])
        assert "".join(chunk.content for chunk in model.stream(history)) == "OK"
        assert [entry[0] for entry in seen] == ["Bearer fixture-first", "Bearer fixture-second"]
        assert "fixture-first" not in repr(model)
        assert "fixture-second" not in repr(model.to_json())
        assert "api_key_file" not in model.model_dump()


@pytest.mark.asyncio
async def test_async_stream_rotation_and_missing_file_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-do-not-use")
    path = tmp_path / "token"
    path.write_text("fixture-first")
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: send_reply(request, seen))) as client:
        model = TokenFileChatDeepSeek(**config(path, http_async_client=client))
        history = [HumanMessage(content="check")]
        first = None
        async for chunk in model.astream(history):
            first = chunk if first is None else first + chunk
        assert first.tool_calls[0]["name"] == "check"
        path.write_text("fixture-second")
        history.extend([first, ToolMessage(content="healthy", tool_call_id="fixture-call")])
        assert (await model.ainvoke(history)).content == "OK"
        path.unlink()
        with pytest.raises(TokenFileError):
            await model.ainvoke(history)
    assert [entry[0] for entry in seen] == ["Bearer fixture-first", "Bearer fixture-second"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": "fixture-static"},
        {"openai_api_key": "fixture-static"},
        {"default_headers": {"Authorization": "fixture-static"}},
        {"api_base": None},
        {"api_base": "https://user:pass@gateway.invalid/v1"},
        {"api_key_file": "relative"},
        {"client": object()},
    ],
)
def test_rejects_implicit_destination_and_authentication_overrides(tmp_path, overrides):
    with pytest.raises(ValueError):
        TokenFileChatDeepSeek(**config(tmp_path / "missing", **overrides))


def test_reflected_deepseek_profile_constructs_without_reading_file(tmp_path):
    profile = {"name": "workload", "use": "deerflow.models.token_file_deepseek:TokenFileChatDeepSeek", **config(tmp_path / "not-read")}
    app = AppConfig.model_validate({"sandbox": {"use": "test"}, "models": [profile]})
    model = create_chat_model("workload", app_config=app, attach_tracing=False)
    assert isinstance(model, TokenFileChatDeepSeek)
