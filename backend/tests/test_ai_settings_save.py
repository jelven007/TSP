"""AI settings failures must leave both persisted and runtime configuration intact."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.api import settings as settings_api
from app.config import settings


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    monkeypatch.setattr(secrets_store, "_cache", None)
    monkeypatch.setattr(secrets_store, "_cache_sig", None)
    for name in type(settings).model_fields:
        if name.startswith("ai_"):
            monkeypatch.setattr(settings, name, getattr(settings, name))
    settings.ai_provider = "openai_compat"
    settings.ai_protocol = "chat_completions"
    settings.ai_base_url = "https://old.test/v1"
    settings.ai_model = "old-model"
    settings.ai_api_key = "old-test-key"
    secrets_store.save({
        "ai_provider": settings.ai_provider,
        "ai_protocol": settings.ai_protocol,
        "ai_base_url": settings.ai_base_url,
        "ai_model": settings.ai_model,
        "ai_api_key": settings.ai_api_key,
        "tickflow_api_key": "unrelated-test-key",
    })
    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize("invalid", [
    {"base_url": "https://ai.test/api/v3https://ai.test/api/v3"},
    {"base_url": "ai.test/v1"},
    {"base_url": "file:///tmp/ai"},
    {"base_url": "https://"},
    {"base_url": "https://ai.test:bad/v1"},
    {"max_output_tokens": -1},
    {"context_window": 0},
    {"round_checkpoint": 3},
    {"provider": "codex_cli", "codex_command": "/tmp/custom-codex"},
])
def test_invalid_save_preserves_disk_runtime_and_existing_key(client, invalid):
    before = secrets_store.load()
    runtime = settings.model_dump()
    result = client.post("/api/settings/ai", json={
        "provider": "openai", "protocol": "responses",
        "base_url": "https://new.test/v1", "model": "new-model",
        "api_key": "", "user_agent": "new-test-agent", **invalid,
    })
    assert result.status_code == 400
    assert secrets_store.load() == before
    assert settings.model_dump() == runtime
    assert client.get("/api/settings").json()["ai_model"] == "old-model"


def test_disk_write_failure_preserves_runtime_and_key(client, monkeypatch):
    before = secrets_store.load()
    runtime = settings.model_dump()

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(secrets_store, "atomic_write_text", fail)
    result = client.post("/api/settings/ai", json={
        "protocol": "responses", "base_url": "https://new.test/v1",
        "model": "new-model", "api_key": "new-test-key",
        "max_output_tokens": 4000, "user_agent": "new-test-agent",
    })
    assert result.status_code == 500
    assert secrets_store.load() == before
    assert settings.model_dump() == runtime


def test_save_normalizes_address_and_preserves_omitted_key(client):
    result = client.post("/api/settings/ai", json={
        "protocol": "responses",
        "base_url": " https://ai.test/api/plan/v3/responses/ ",
        "model": "new-model",
    })
    assert result.status_code == 200
    stored = secrets_store.load()
    assert stored["ai_base_url"] == "https://ai.test/api/plan/v3"
    assert settings.ai_base_url == stored["ai_base_url"]
    assert stored["ai_api_key"] == "old-test-key"
    assert stored["tickflow_api_key"] == "unrelated-test-key"
    assert client.get("/api/settings").json()["ai_protocol"] == "responses"


def test_explicit_empty_key_is_persisted_with_the_other_settings(client, monkeypatch):
    calls = []
    real_write = secrets_store.atomic_write_text

    def write(*args, **kwargs):
        calls.append(args[0])
        return real_write(*args, **kwargs)

    monkeypatch.setattr(secrets_store, "atomic_write_text", write)
    result = client.post("/api/settings/ai", json={"api_key": "", "model": "new-model"})
    assert result.status_code == 200
    assert len(calls) == 1
    assert not secrets_store.get_ai_key()
    assert not settings.ai_api_key
    assert secrets_store.load()["tickflow_api_key"] == "unrelated-test-key"
