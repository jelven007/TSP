"""Full API bootstrap precedes account use and releases resources on failure."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from app.multitenancy import lifespan as full
from fastapi import FastAPI


@pytest.mark.asyncio
@pytest.mark.parametrize("bootstrap_fails", [False, True])
async def test_full_api_initializes_admin_with_runtime_connection(monkeypatch, bootstrap_fails):
    calls = []
    connection = object()

    @contextmanager
    def control_connection():
        calls.append("connection")
        yield connection

    class StopStartupError(Exception):
        pass

    db = SimpleNamespace(
        open=lambda: calls.append("open"),
        close=lambda: calls.append("close"),
        control=SimpleNamespace(connection=control_connection),
    )

    def bootstrap(control):
        assert control is connection
        calls.append("bootstrap")
        if bootstrap_fails:
            raise StopStartupError
        return True

    def identity(_db):
        assert _db is db
        calls.append("identity")
        raise StopStartupError

    monkeypatch.setattr(full.settings, "service_role", "api")
    monkeypatch.setattr(full, "CellSettings", lambda: object())
    monkeypatch.setattr(full, "Databases", lambda _settings: db)
    monkeypatch.setattr(full, "bootstrap_full_identity", bootstrap)
    monkeypatch.setattr(full, "AccountIdentity", identity)
    with pytest.raises(StopStartupError):
        async with full.lifespan(FastAPI()):
            pytest.fail("initialization should stop before other services")

    assert calls == ["open", "connection", "bootstrap",
                     *([] if bootstrap_fails else ["identity"]), "close"]


def test_full_initialization_enables_registration_and_recovery_without_env(monkeypatch):
    calls = []

    class Identity:
        def set_registration_secret(self, value, *, only_if_missing):
            calls.append((value, only_if_missing))

    monkeypatch.setattr(full.settings, "auth_registration_secret", "")
    full._bootstrap_registration_secret(Identity())
    assert calls == [("天王盖地虎", True)]


def test_registration_bootstrap_failure_stops_full_initialization(monkeypatch):
    class Identity:
        def set_registration_secret(self, *_args, **_kwargs):
            raise RuntimeError("identity storage unavailable")

    monkeypatch.setattr(full.settings, "auth_registration_secret", "")
    with pytest.raises(RuntimeError, match="storage unavailable"):
        full._bootstrap_registration_secret(Identity())


@pytest.mark.parametrize(
    ("base_url", "secure"),
    [("http://127.0.0.1:3028", False), ("http://localhost:3028", False),
     ("https://testserver", True), ("http://example.com", True)],
)
def test_full_login_cookie_works_in_loopback_development(base_url, secure):
    from urllib.parse import urlsplit

    from app.multitenancy.routes import _cookie
    from starlette.requests import Request
    from starlette.responses import Response

    url = urlsplit(base_url)
    app = FastAPI()
    app.state.account_identity = SimpleNamespace(db=SimpleNamespace(
        settings=SimpleNamespace(session_ttl_seconds=86400),
    ))
    request = Request({
        "type": "http", "scheme": url.scheme, "path": "/", "query_string": b"",
        "server": (url.hostname, url.port or 443), "headers": [(b"host", url.netloc.encode())],
        "app": app,
    })
    response = Response()
    _cookie(response, request, "session")
    assert ("Secure" in response.headers["set-cookie"]) is secure
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
