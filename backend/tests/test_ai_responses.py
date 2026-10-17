"""Real SDK wire contracts for the opt-in Responses protocol (no external AI)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from app import secrets_store
from app.api import settings as settings_api
from app.config import settings
from app.custom.assistant import chat_service, streaming
from app.custom.assistant import tools as assistant_tools
from app.services import ai_provider

MESSAGES = [{"role": "user", "content": "检查数据"}]
TOOLS = [{"type": "function", "function": {
    "name": "check_data_coverage", "description": "Coverage",
    "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}},
}}]
CALL = {"id": "fc_1", "type": "function_call", "call_id": "call_1",
        "name": "check_data_coverage", "arguments": '{"symbol":"600519.SH"}',
        "status": "completed"}
REASONING = {"id": "rs_1", "type": "reasoning", "summary": [],
             "encrypted_content": "opaque-test-reasoning"}


def text_item(text="完成"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def response(output=None, *, status="completed", reason=None):
    return {
        "id": "resp_1", "object": "response", "created_at": 1, "model": "test-model",
        "status": status, "output": [text_item()] if output is None else output,
        "error": None, "incomplete_details": {"reason": reason} if reason else None,
    }


def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def terminal(payload):
    return {"type": f"response.{payload['status']}", "response": payload, "sequence_number": 2}


@pytest.fixture
def wire(monkeypatch):
    saved = {"ai_protocol": "responses", "ai_provider": "openai", "ai_model": "test-model",
             "ai_reasoning_effort": "high", "ai_api_key": "test-key"}
    monkeypatch.setattr(secrets_store, "load", lambda: dict(saved))
    monkeypatch.setattr(settings, "ai_max_output_tokens", 5000)
    monkeypatch.setattr(settings, "ai_context_window", 128000)
    requests, clients, replies = [], [], []

    async def handle(request):
        requests.append((request.url.path, json.loads(request.content)))
        result = replies.pop(0)
        if isinstance(result, httpx.Response):
            return result
        if isinstance(result, bytes):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=result)
        return httpx.Response(200, json=result)

    def client(key, timeout):
        result = AsyncOpenAI(
            api_key=key, base_url="https://ai.test/v1", timeout=timeout, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(result)
        return result

    monkeypatch.setattr(ai_provider, "_openai_client", client)
    return saved, requests, clients, replies


async def test_text_selects_responses_and_maps_budget(wire):
    _, requests, clients, replies = wire
    replies.append(response())
    assert await ai_provider.generate_ai_text(MESSAGES, max_tokens=9000) == "完成"
    path, body = requests[0]
    assert path == "/v1/responses"
    assert body["input"] == MESSAGES
    assert body["max_output_tokens"] == 5000 and "max_tokens" not in body
    assert body["reasoning"] == {"effort": "high"}
    assert body["store"] is False and "previous_response_id" not in body
    assert all(c.is_closed() for c in clients)


async def test_tools_replay_reasoning_and_call_ids_without_server_history(wire):
    _, requests, clients, replies = wire
    replies.extend([response([REASONING, CALL]), response()])
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"ok": True, "result": {"rows": 1}}

    messages = await ai_provider.generate_ai_text_with_tools(
        MESSAGES, TOOLS, execute_tool=execute, max_tokens=None,
    )
    assert executed == [("check_data_coverage", {"symbol": "600519.SH"})]
    assert messages[-1]["content"] == "完成"
    assert messages[-2]["role"] == "tool" and messages[-2]["tool_call_id"] == "call_1"
    first, second = [body for _, body in requests]
    assert first["tools"][0]["name"] == "check_data_coverage"
    assert first["tools"][0]["strict"] is False
    assert "max_output_tokens" not in first
    assert "reasoning.encrypted_content" in first["include"]
    assert second["input"][1:3] == [REASONING, CALL]
    assert second["input"][3]["type"] == "function_call_output"
    assert second["input"][3]["call_id"] == "call_1"
    assert all(c.is_closed() for c in clients)


@pytest.mark.parametrize("payload,match", [
    (response([CALL], status="incomplete", reason="max_output_tokens"), "长度上限"),
    (response([CALL], status="incomplete", reason="content_filter"), "不完整"),
    (response([CALL], status="failed"), "失败"),
    (response([CALL], status="in_progress"), "终态"),
    (response([]), "正文"),
    (response([REASONING]), "正文"),
    (response([{**CALL, "arguments": '{"symbol":'}]), "工具参数"),
    (response([{**CALL, "arguments": "[]"}]), "工具参数"),
    (response([{**CALL, "call_id": ""}]), "工具调用"),
    (response([{**CALL, "name": "unknown_tool"}]), "工具调用"),
    (response([CALL, CALL]), "工具调用"),
    (response([{**CALL, "status": "in_progress"}]), "工具调用"),
])
async def test_invalid_response_never_executes_tools(wire, payload, match):
    _, _, clients, replies = wire
    replies.append(payload)

    async def execute(*args):
        pytest.fail("must not execute incomplete or invalid calls")

    with pytest.raises(RuntimeError, match=match):
        await ai_provider.generate_ai_text_with_tools(MESSAGES, TOOLS, execute_tool=execute)
    assert all(c.is_closed() for c in clients)


async def test_stream_emits_deltas_once_and_final_text_without_deltas(wire):
    _, _, clients, replies = wire
    replies.append(sse(
        {"type": "response.output_text.delta", "delta": "完", "output_index": 0, "content_index": 0},
        {"type": "response.output_text.delta", "delta": "成", "output_index": 0, "content_index": 0},
        {"type": "response.output_text.done", "text": "完成", "output_index": 0, "content_index": 0},
        terminal(response()),
    ))
    assert [x async for x in ai_provider.stream_ai_text(MESSAGES)] == ["完", "成"]
    replies.append(sse(terminal(response())))
    assert [x async for x in ai_provider.stream_ai_text(MESSAGES)] == ["完成"]
    assert all(c.is_closed() for c in clients)


@pytest.mark.parametrize("tail,match", [
    ([], "终态"),
    ([terminal(response([CALL], status="incomplete", reason="max_output_tokens"))], "长度上限"),
    ([terminal(response([CALL], status="incomplete", reason="content_filter"))], "不完整"),
    ([{"type": "error", "code": "server_error", "message": "unavailable"}], "失败"),
])
async def test_stream_never_returns_tools_on_truncation_or_failure(wire, tail, match):
    _, _, clients, replies = wire
    replies.append(sse({"type": "response.output_item.done", "item": CALL, "output_index": 0}, *tail))
    events = []
    with pytest.raises(RuntimeError, match=match):
        async for event in streaming.stream_openai_round(MESSAGES, TOOLS):
            events.append(event)
    assert not any(e["type"] == "round_end" for e in events)
    assert all(c.is_closed() for c in clients)


async def test_assistant_replays_responses_and_keeps_tool_contract(wire, monkeypatch):
    _, requests, clients, replies = wire
    replies.extend([sse(terminal(response([REASONING, CALL]))), sse(terminal(response()))])
    monkeypatch.setattr(assistant_tools, "build_tool_schemas", lambda: TOOLS)

    async def execute(name, args, context):
        assert name == "check_data_coverage"
        return {"ok": True, "result": {"rows": 1}}

    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", execute)
    events = [json.loads(line) async for line in chat_service.chat_stream(history=MESSAGES)]
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "delta", "done"]
    assert requests[1][1]["input"][-3:-1] == [REASONING, CALL]
    assert requests[1][1]["input"][-1]["call_id"] == "call_1"
    assert all(c.is_closed() for c in clients)


async def test_protocol_failure_has_no_chat_fallback_and_closes_client(wire):
    _, requests, clients, replies = wire
    replies.append(httpx.Response(404, json={"error": {"message": "Responses unavailable"}}))
    with pytest.raises(RuntimeError, match="404"):
        await ai_provider.generate_ai_text(MESSAGES)
    assert len(requests) == 1 and requests[0][0] == "/v1/responses"
    assert all(c.is_closed() for c in clients)


async def test_optional_parameter_retry_is_bounded(wire):
    _, requests, clients, replies = wire
    replies.extend([
        httpx.Response(400, json={"error": {"param": "temperature", "message": "unsupported temperature"}}),
        httpx.Response(400, json={"error": {"param": "reasoning.effort", "message": "unsupported reasoning"}}),
        response(),
    ])
    assert await ai_provider.generate_ai_text(MESSAGES) == "完成"
    assert len(requests) == 3
    assert "temperature" not in requests[1][1]
    assert "reasoning" not in requests[2][1]
    assert all(c.is_closed() for c in clients)


async def test_cancel_closes_pending_response_stream(wire):
    _, _, clients, replies = wire
    entered, closed = asyncio.Event(), asyncio.Event()

    class Pending(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"type": "response.output_text.delta", "delta": "一半",
                       "output_index": 0, "content_index": 0})
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    replies.append(httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Pending()))

    async def consume():
        return [x async for x in ai_provider.stream_ai_text(MESSAGES)]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert all(c.is_closed() for c in clients)


def test_settings_preserve_omitted_protocol_validate_and_clear(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    for name in type(settings).model_fields:
        if name.startswith("ai_"):
            monkeypatch.setattr(settings, name, getattr(settings, name))
    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app) as client:
        first = client.post("/api/settings/ai", json={"protocol": "responses", "model": "test-model"})
        assert first.status_code == 200
        assert first.json()["ai_protocol"] == "responses"
        legacy = client.post("/api/settings/ai", json={"model": "another"})
        assert legacy.json()["ai_protocol"] == "responses"
        assert client.get("/api/settings").json()["ai_protocol"] == "responses"
        bad = client.post("/api/settings/ai", json={"protocol": "auto", "model": "bad"})
        assert bad.status_code == 422
        assert secrets_store.load()["ai_model"] == "another"
        assert client.delete("/api/settings/ai").status_code == 200
        assert ai_provider.current_ai_protocol() == "chat_completions"


def test_default_protocol_and_normalize_complete_responses_url(monkeypatch):
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    monkeypatch.setattr(settings, "ai_protocol", "chat_completions")
    assert ai_provider.current_ai_protocol() == "chat_completions"
    assert ai_provider.normalize_openai_base_url("https://ai.test/v1/responses/") == "https://ai.test/v1"


async def test_legacy_chat_selection_still_uses_chat_endpoint(wire):
    saved, requests, _, replies = wire
    saved.pop("ai_protocol")
    replies.append({"id": "chat_1", "object": "chat.completion", "created": 1, "model": "test-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "兼容"}}]})
    assert await ai_provider.generate_ai_text(MESSAGES) == "兼容"
    assert requests[0][0] == "/v1/chat/completions"


async def test_consumer_close_releases_stream_immediately(wire):
    _, _, clients, replies = wire
    replies.append(sse(
        {"type": "response.output_text.delta", "delta": "完成", "output_index": 0, "content_index": 0},
        terminal(response()),
    ))
    stream = ai_provider.stream_ai_text(MESSAGES)
    assert await anext(stream) == "完成"
    await stream.aclose()
    assert all(c.is_closed() for c in clients)


async def test_partial_text_then_failure_is_an_error_not_success(wire):
    _, _, clients, replies = wire
    replies.append(sse(
        {"type": "response.output_text.delta", "delta": "一半", "output_index": 0, "content_index": 0},
        terminal(response([text_item("一半")], status="incomplete", reason="max_output_tokens")),
    ))
    text = []
    with pytest.raises(RuntimeError, match="长度上限"):
        async for piece in ai_provider.stream_ai_text(MESSAGES):
            text.append(piece)
    assert text == ["一半"]
    assert all(c.is_closed() for c in clients)


async def test_interleaved_conversations_do_not_share_native_output(wire):
    _, requests, _, replies = wire
    one_ready, two_ready = asyncio.Event(), asyncio.Event()
    call_two = {**CALL, "id": "fc_2", "call_id": "call_2"}
    reasoning_two = {**REASONING, "id": "rs_2", "encrypted_content": "second-conversation"}
    replies.extend([response([REASONING, CALL]), response([reasoning_two, call_two]),
                    response(), response()])

    async def execute_one(*args):
        one_ready.set()
        await two_ready.wait()
        return {"ok": True}

    async def execute_two(*args):
        await one_ready.wait()
        two_ready.set()
        return {"ok": True}

    await asyncio.gather(
        ai_provider.generate_ai_text_with_tools(MESSAGES, TOOLS, execute_tool=execute_one),
        ai_provider.generate_ai_text_with_tools(MESSAGES, TOOLS, execute_tool=execute_two),
    )
    replays = [body["input"][1:-1] for _, body in requests[2:]]
    assert [REASONING, CALL] in replays
    assert [reasoning_two, call_two] in replays
