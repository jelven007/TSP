"""The settings connection probe must leave room for a reasoning model's answer."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from app import secrets_store
from app.api import strategy
from app.config import settings
from app.services import ai_provider


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("cap", [256, 4096])
def test_connection_probe_allows_reasoning_and_respects_configured_cap(monkeypatch, protocol, cap):
    stored = {
        "ai_protocol": protocol, "ai_provider": "openai_compat",
        "ai_model": "test-reasoning-model", "ai_api_key": "test-key",
    }
    monkeypatch.setattr(secrets_store, "load", lambda: dict(stored))
    monkeypatch.setattr(settings, "ai_max_output_tokens", cap)
    monkeypatch.setattr(settings, "ai_context_window", 128000)
    requests, clients = [], []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        budget = body.get("max_output_tokens", body.get("max_tokens", 0))
        enough = budget >= 64
        if protocol == "responses":
            output = [{
                "id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "OK", "annotations": []}],
            }] if enough else [{"id": "rs_1", "type": "reasoning", "summary": []}]
            return httpx.Response(200, json={
                "id": "resp_1", "object": "response", "created_at": 1,
                "model": stored["ai_model"],
                "status": "completed" if enough else "incomplete", "output": output,
                "incomplete_details": None if enough else {"reason": "length"},
            })
        return httpx.Response(200, json={
            "id": "chat_1", "object": "chat.completion", "created": 1,
            "model": stored["ai_model"], "choices": [{
                "index": 0, "finish_reason": "stop" if enough else "length",
                "message": {"role": "assistant", "content": "OK" if enough else "",
                            "reasoning_content": "" if enough else "thinking"},
            }],
        })

    def client(key, timeout):
        result = AsyncOpenAI(
            api_key=key, base_url="https://ai.test/v1", timeout=timeout, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(result)
        return result

    monkeypatch.setattr(ai_provider, "_openai_client", client)
    app = FastAPI()
    app.include_router(strategy.router)
    with TestClient(app) as test_client:
        response = test_client.post("/api/strategies/ai/test")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "model": stored["ai_model"], "response": "OK"}
    assert len(requests) == 1
    budget = requests[0].get("max_output_tokens", requests[0].get("max_tokens"))
    assert 64 <= budget <= cap
    for sdk_client in clients:
        asyncio.run(sdk_client.close())
