"""Native async SDK credential resolution must keep file reads off-loop."""

import asyncio
import json

import httpx
import pytest

from deerflow.models.token_file import TokenFileChatOpenAI
from deerflow.models.token_file_deepseek import TokenFileChatDeepSeek


@pytest.mark.asyncio
@pytest.mark.parametrize("model_class,endpoint", [(TokenFileChatOpenAI, "base_url"), (TokenFileChatDeepSeek, "api_base")])
async def test_async_model_reads_rotating_file_without_blocking_event_loop(tmp_path, model_class, endpoint):
    path = tmp_path / "token"
    await asyncio.to_thread(path.write_text, "fixture-first")
    seen = []

    def send(request):
        seen.append(request.headers["authorization"])
        if json.loads(request.content).get("stream"):
            chunk = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "gateway-model", "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1, "model": "gateway-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        model = await asyncio.to_thread(model_class, model="gateway-model", api_key_file=path, http_async_client=client, max_retries=0, **{endpoint: "http://gateway.invalid/v1"})
        assert (await model.ainvoke("first")).content == "OK"
        await asyncio.to_thread(path.write_text, "fixture-second")
        assert "".join([chunk.content async for chunk in model.astream("second")]) == "OK"
    assert seen == ["Bearer fixture-first", "Bearer fixture-second"]
