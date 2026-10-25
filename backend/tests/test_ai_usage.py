"""Admission, persistence and adapter evidence without external model calls."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import ai_provider, ai_usage


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "platform_config_dir", None)
    monkeypatch.setattr(settings, "multitenancy_enabled", False)
    for name in ("daily_request_limit", "user_daily_request_limit", "task_request_limit",
                 "daily_token_limit", "daily_cost_limit_micros"):
        monkeypatch.setattr(settings, f"ai_{name}", 0)
    monkeypatch.setattr(settings, "ai_pricing_json", "{}")
    monkeypatch.setattr(ai_provider, "current_ai_provider", lambda: "openai_compat")


async def test_create_records_usage_and_never_records_prompt_or_secret():
    async def create(**kwargs):
        return SimpleNamespace(id="upstream-1", choices=[], usage=SimpleNamespace(
            prompt_tokens=12, completion_tokens=3))
    await ai_usage.metered_create(
        create, protocol="chat_completions", model="test-model",
        messages=[{"content": "PRIVATE_PROMPT"}], api_key="SECRET",
    )
    report = ai_usage.usage_summary()
    assert report["known_tokens"] == 15
    assert report["unknown_usage_calls"] == 0
    assert report["unknown_cost_calls"] == 1
    assert report["records"][0]["cost_micros"] is None
    assert report["records"][0]["upstream_id"] == "upstream-1"
    assert "PRIVATE_PROMPT" not in ai_usage._path().read_text()
    assert "SECRET" not in ai_usage._path().read_text()


def test_atomic_request_limit_includes_crashed_pending_calls(monkeypatch):
    monkeypatch.setattr(settings, "ai_daily_request_limit", 2)
    def attempt(_):
        try:
            ai_usage.Reservation("openai_compat", "model", "chat_completions", 100)
            return True
        except RuntimeError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 2
    assert ai_usage.usage_summary()["pending_calls"] == 2
    with pytest.raises(RuntimeError, match="次数"):
        ai_usage.Reservation("openai_compat", "model", "chat_completions", 100)


def test_unknown_tokens_block_configured_budget(monkeypatch):
    monkeypatch.setattr(settings, "ai_context_window", 100)
    monkeypatch.setattr(settings, "ai_max_output_tokens", 10)
    monkeypatch.setattr(settings, "ai_daily_token_limit", 500)
    reservation = ai_usage.Reservation("openai_compat", "model", "chat_completions", 10)
    reservation.finish("completed")
    assert ai_usage.usage_summary()["records"][0]["usage"] is None
    with pytest.raises(RuntimeError, match="未知用量"):
        ai_usage.Reservation("openai_compat", "model", "chat_completions", 10)


def test_cost_requires_matching_evidenced_price_and_records_micros(monkeypatch):
    monkeypatch.setattr(settings, "ai_daily_cost_limit_micros", 10000)
    monkeypatch.setattr(settings, "ai_context_window", 100)
    monkeypatch.setattr(settings, "ai_max_output_tokens", 10)
    monkeypatch.setattr(ai_provider.secrets_store, "get_ai_config", lambda *args: "https://model.invalid/v1")
    with pytest.raises(RuntimeError, match="价格"):
        ai_usage.Reservation("openai_compat", "model", "chat_completions", 10)
    monkeypatch.setattr(settings, "ai_pricing_json", json.dumps({"model": {
        "provider": "openai_compat", "base_url": "https://model.invalid/v1", "currency": "USD",
        "source": "operator price sheet 2026-10-25", "input_per_million": "1.5",
        "output_per_million": "2",
    }}))
    reservation = ai_usage.Reservation("openai_compat", "model", "chat_completions", 10)
    reservation.finish("completed", {"input_tokens": 10, "output_tokens": 5})
    row = ai_usage.usage_summary()["records"][0]
    assert row["cost_micros"] == 25
    assert row["price_source"] == "operator price sheet 2026-10-25"
    with pytest.raises(RuntimeError, match="CLI"):
        ai_usage.Reservation("codex_cli", "model", "codex_cli", None)


async def test_stream_captures_usage_only_terminal_chunk_and_closes():
    class Stream:
        closed = False
        async def __aiter__(self):
            yield SimpleNamespace(id="stream-1", choices=[1], usage=None)
            yield SimpleNamespace(id="stream-1", choices=[], usage={
                "prompt_tokens": 10, "completion_tokens": 4})
        async def close(self):
            self.closed = True
    stream = Stream()
    async def create(**kwargs):
        return stream
    result = await ai_usage.metered_create(create, protocol="chat_completions", model="model", stream=True)
    assert len([chunk async for chunk in result]) == 2
    assert stream.closed
    assert ai_usage.usage_summary()["known_tokens"] == 14
    assert ai_usage.usage_summary()["pending_calls"] == 0


async def test_cancelled_upstream_is_recorded_and_does_not_leak_error_text():
    async def create(**kwargs):
        raise asyncio.CancelledError("SECRET-CANCEL")
    with pytest.raises(asyncio.CancelledError):
        await ai_usage.metered_create(create, protocol="responses", model="model")
    row = ai_usage.usage_summary()["records"][0]
    assert row["status"] == "cancelled"
    assert row["failure_kind"] == "CancelledError"
    assert "SECRET-CANCEL" not in ai_usage._path().read_text()


def test_tenant_scope_cannot_fall_back_to_public_files(monkeypatch):
    monkeypatch.setattr(settings, "multitenancy_enabled", True)
    monkeypatch.setattr(settings, "service_role", "api")
    with pytest.raises(RuntimeError, match="租户上下文"):
        ai_usage.Reservation("openai_compat", "model", "chat_completions", 10)
    assert not ai_usage._path().exists()


def test_budget_reserves_saved_context_window_and_disables_hidden_retries(monkeypatch):
    monkeypatch.setattr(settings, "ai_context_window", 100)
    monkeypatch.setattr(settings, "ai_daily_token_limit", 500)
    monkeypatch.setattr(ai_provider, "current_ai_context_window", lambda: 1000)
    with pytest.raises(RuntimeError, match="Token"):
        ai_usage.Reservation("openai_compat", "model", "chat_completions", 10)
    client = ai_provider._openai_client("not-a-real-key", 1)
    assert client.max_retries == 0
    asyncio.run(client.close())
    monkeypatch.setattr(settings, "ai_daily_token_limit", 0)
    client = ai_provider._openai_client("not-a-real-key", 1)
    assert client.max_retries == 2
    asyncio.run(client.close())


async def test_partial_stream_close_records_cancelled_unknown_and_releases_resource():
    class Stream:
        closed = False

        async def __aiter__(self):
            yield SimpleNamespace(id="partial", choices=[1], usage=None)
            raise AssertionError("consumer must stop before the second chunk")

        async def close(self):
            self.closed = True

    stream = Stream()

    async def create(**kwargs):
        return stream

    result = await ai_usage.metered_create(
        create, protocol="chat_completions", model="model", stream=True,
    )
    iterator = result.__aiter__()
    await anext(iterator)
    await iterator.aclose()
    assert stream.closed
    row = ai_usage.usage_summary()["records"][0]
    assert row["status"] == "cancelled" and row["usage"] is None
    assert ai_usage.usage_summary()["pending_calls"] == 0
