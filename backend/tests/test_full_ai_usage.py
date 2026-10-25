"""Real SQL tenant admission and streaming middleware context propagation."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.config import settings
from app.custom.assistant.routes import build_router
from app.multitenancy import access
from app.multitenancy.documents import TenantDocuments
from app.services import ai_usage


def test_sql_budget_serializes_without_local_fallback(env, monkeypatch):
    _, user = register(env)
    documents = TenantDocuments(env["db"], env["identity"].context(user, None, "budget"))
    monkeypatch.setattr(settings, "ai_daily_request_limit", 1)
    monkeypatch.setattr(ai_usage, "_path", lambda: (_ for _ in ()).throw(AssertionError("local fallback")))
    def attempt(_):
        with ai_usage.ai_usage_scope(documents):
            try:
                ai_usage.Reservation("openai_compat", "model", "chat_completions", 10)
                return True
            except RuntimeError as exc:
                assert "次数" in str(exc)
                return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1
    with ai_usage.ai_usage_scope(documents):
        assert ai_usage.usage_summary()["tenant_calls"] == 1
        assert ai_usage.usage_summary()["pending_calls"] == 1


def test_streaming_scope_and_http_usage_separate_tenants_and_users(env, monkeypatch):
    first_token, first = register(env)
    second_token, second = register(env)
    monkeypatch.setattr(settings, "ai_daily_request_limit", 0)
    monkeypatch.setattr(ai_usage, "_path", lambda: (_ for _ in ()).throw(AssertionError("local fallback")))
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.middleware("http")(access.middleware)
    app.add_exception_handler(AccessError, access.access_error)
    app.include_router(build_router())

    class Stream:
        async def __aiter__(self):
            yield SimpleNamespace(id="response-id", usage={"prompt_tokens": 12, "completion_tokens": 4})
        async def close(self):
            pass

    async def create(**kwargs):
        return Stream()

    @app.post("/api/custom/assistant/usage-probe")
    async def probe():
        async def generate():
            stream = await ai_usage.metered_create(
                create, protocol="chat_completions", model="model", stream=True,
            )
            async for _ in stream:
                yield "done"
        return StreamingResponse(generate())

    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", first_token)
        assert client.post("/api/custom/assistant/usage-probe").text == "done"
        report = client.get("/api/custom/assistant/usage").json()
        assert report["user_calls"] == report["tenant_calls"] == 1
        assert report["known_tokens"] == 16
        client.cookies.set("tf_session", second_token)
        report = client.get("/api/custom/assistant/usage").json()
        assert report["user_calls"] == report["tenant_calls"] == 0
        assert report["records"] == []
        denied = client.get("/api/custom/assistant/usage", headers={"X-Tenant-Id": first["default_tenant_id"]})
        assert denied.status_code == 403
        assert first["id"] != second["id"]
