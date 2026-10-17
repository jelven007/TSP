"""Assistant history CAS, retention and private storage contracts."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cell.context import AccessError
from app.custom.assistant.routes import build_router
from app.custom.assistant.sessions import SessionStore
from app.multitenancy.access import access_error


def session(title="第一轮", identifier="one"):
    return {
        "id": identifier, "title": title, "createdAt": 100,
        "messages": [{"id": "m", "role": "user", "content": title, "ts": 101}],
    }


def test_local_accounts_revision_delete_restart_and_stale_resurrection(tmp_path):
    first = SessionStore(data_dir=tmp_path, owner="first")
    second = SessionStore(data_dir=tmp_path, owner="second")
    assert first.read() == {"revision": 0, "sessions": []}
    assert first.write([session()], 0)["revision"] == 1
    assert second.read()["sessions"] == []
    assert SessionStore(data_dir=tmp_path, owner="first").read()["sessions"] == [session()]
    assert first.write([], 1) == {"revision": 2, "sessions": []}
    with pytest.raises(AccessError) as exc:
        first.write([session("过期草稿")], 1)
    assert exc.value.status == 409
    assert first.read() == {"revision": 2, "sessions": []}


def test_local_concurrent_writers_compare_inside_transaction(tmp_path):
    def write(n):
        try:
            return SessionStore(data_dir=tmp_path, owner="first").write([session(str(n))], 0)
        except AccessError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(write, range(4)))
    assert result.count(409) == 3
    assert SessionStore(data_dir=tmp_path, owner="first").read()["revision"] == 1


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.services import auth

    monkeypatch.setattr(auth, "current_user", lambda token: {"id": token} if token else None)
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.add_exception_handler(AccessError, access_error)
    app.include_router(build_router())
    with TestClient(app) as test_client:
        yield test_client


def test_http_owner_revision_and_capacity(client):
    path = "/api/custom/assistant/sessions"
    client.cookies.set("tf_session", "first")
    assert client.get(path).json() == {"revision": 0, "sessions": []}
    result = client.put(path, json={"revision": 0, "sessions": [session()]})
    assert result.status_code == 200, result.text
    assert result.json()["revision"] == 1
    assert client.get(path).headers["cache-control"] == "no-store"
    assert client.put(path, json={"revision": 0, "sessions": []}).status_code == 409
    assert client.put(path, json={"revision": 1, "sessions": [session()] * 21}).status_code == 422
    assert client.put(path, json={"revision": 1, "sessions": [session()] * 2}).status_code == 422
    oversized = session()
    oversized["messages"] *= 201
    assert client.put(path, json={"revision": 1, "sessions": [oversized]}).status_code == 422
    invalid = session()
    invalid["messages"][0]["content"] = {}
    assert client.put(path, json={"revision": 1, "sessions": [invalid]}).status_code == 422
    client.cookies.set("tf_session", "second")
    assert client.get(path).json()["sessions"] == []
    assert client.get(path, headers={"Authorization": "Bearer any-api-token"}).status_code == 403
    client.cookies.set("tf_session", "first")
    assert client.put(path, json={"revision": 1, "sessions": []}).json()["revision"] == 2
    assert client.get(path).json()["sessions"] == []


def test_total_bytes_rejected_without_replacing_saved_history(tmp_path):
    store = SessionStore(data_dir=tmp_path, owner="first")
    store.write([session()], 0)
    huge = [session(identifier=str(n)) for n in range(20)]
    for row in huge:
        row["messages"][0]["content"] = "新" * 1000
        row["messages"] *= 200
    with pytest.raises(AccessError) as exc:
        store.write(huge, 1)
    assert exc.value.status == 413
    assert store.read()["sessions"] == [session()]


def test_missing_tenant_never_falls_back_to_local(client, tmp_path):
    client.app.state.account_identity = object()
    assert client.get("/api/custom/assistant/sessions").status_code == 409
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_stored_incomplete_generation_and_confirmation_are_inert(client):
    row = session()
    row["messages"] += [
        {"id": "answer", "role": "assistant", "content": "一半", "streaming": True, "ts": 102},
        {"id": "tools", "role": "footprint", "ts": 103, "calls": [
            {"callId": "pending", "name": "add_to_watchlist", "args": {},
             "status": "awaiting", "confirm": {"label": "添加", "risk": "修改"}},
        ]},
    ]
    path = "/api/custom/assistant/sessions"
    assert client.put(path, json={"revision": 0, "sessions": [row]}).status_code == 200
    messages = client.get(path).json()["sessions"][0]["messages"]
    assert messages[1]["streaming"] is False
    assert messages[2]["calls"][0]["status"] == "denied"
