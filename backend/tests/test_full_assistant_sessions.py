"""Assistant history on real PostgreSQL with middleware and RLS enabled."""
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_assistant_sessions import session
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.custom.assistant.routes import build_router
from app.custom.assistant.sessions import SessionStore
from app.multitenancy import access
from app.multitenancy.documents import TenantDocuments


def test_account_and_tenant_scopes_cas_and_delete(env, monkeypatch):
    first_token, first = register(env)
    second_token, second = register(env)
    with env["control"].transaction():
        env["control"].execute("INSERT INTO control.memberships VALUES(%s,%s,'member')",
                               (first["default_tenant_id"], second["id"]))
        env["control"].execute("INSERT INTO control.memberships VALUES(%s,%s,'member')",
                               (second["default_tenant_id"], first["id"]))
    monkeypatch.setattr(SessionStore, "_local", lambda _: (_ for _ in ()).throw(AssertionError("local fallback")))
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.middleware("http")(access.middleware)
    app.add_exception_handler(AccessError, access.access_error)
    app.include_router(build_router())
    path = "/api/custom/assistant/sessions"
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", first_token)
        assert client.put(path, json={"revision": 0, "sessions": [session()]}).status_code == 200
        client.cookies.set("tf_session", second_token)
        # Another member of the same tenant must not see the account's conversation.
        shared = {"X-Tenant-Id": first["default_tenant_id"]}
        assert client.get(path, headers=shared).json() == {"revision": 0, "sessions": []}
        assert client.put(path, headers=shared, json={"revision": 0, "sessions": [session("second")]}).status_code == 200
        client.cookies.set("tf_session", first_token)
        assert client.get(path).json()["sessions"][0]["title"] == "第一轮"
        other = {"X-Tenant-Id": second["default_tenant_id"]}
        assert client.get(path, headers=other).json() == {"revision": 0, "sessions": []}
        assert client.put(path, json={"revision": 1, "sessions": []}).json()["revision"] == 2
        assert client.put(path, json={"revision": 1, "sessions": [session()]}).status_code == 409
        assert client.get(path).json() == {"revision": 2, "sessions": []}
        env["identity"].logout(first_token)
        assert client.get(path).status_code == 401


def test_concurrent_account_collection_updates_use_tenant_transaction(env):
    _, user = register(env)
    context = env["identity"].context(user, None, "sessions-concurrency")

    def write(n):
        store = SessionStore(documents=TenantDocuments(env["db"], context))
        try:
            return store.write([session(str(n))], 0)
        except AccessError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))
    assert results.count(409) == 3
    restarted = SessionStore(documents=TenantDocuments(env["db"], context)).read()
    assert restarted["revision"] == 1 and len(restarted["sessions"]) == 1
