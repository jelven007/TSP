"""Tenant strategy freshness reads keep platform administration private."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.api import intraday
from app.multitenancy.platform import PlatformAdmin, market_read_access, market_read_path

STATUS_PATH = "/api/intraday/minute-refresh/status"


@pytest.fixture
def minute_platform(monkeypatch):
    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "test-platform-read-token")
    app = FastAPI()
    app.include_router(intraday.router)

    @app.middleware("http")
    async def platform_access(request: Request, call_next):
        if not market_read_access(request):
            return JSONResponse({"detail": "platform_read_denied"}, status_code=403)
        assert "cookie" not in request.headers
        assert "x-tenant-id" not in request.headers
        return await call_next(request)

    return app


@pytest.mark.parametrize("healthy", [False, True])
def test_platform_minute_read_exposes_only_availability_and_freshness(minute_platform, healthy):
    minute_platform.state.minute_refresh = SimpleNamespace(status=lambda: {
        "healthy": healthy,
        "running": True,
        "provider": "private-provider",
        "last_error": "private collector diagnostic",
        "api_key": "test-sensitive-value",
    })
    with TestClient(minute_platform, headers={"Authorization": "Bearer test-platform-read-token"}) as client:
        response = client.get(STATUS_PATH)
    assert response.status_code == 200
    assert response.json() == {"available": True, "healthy": healthy}
    assert not market_read_path("POST", STATUS_PATH)
    assert not market_read_path("PUT", STATUS_PATH)
    assert not market_read_path("DELETE", STATUS_PATH)


def test_absent_minute_collector_is_unavailable_not_healthy(minute_platform):
    with TestClient(minute_platform, headers={"Authorization": "Bearer test-platform-read-token"}) as client:
        response = client.get(STATUS_PATH)
    assert response.status_code == 200
    assert response.json() == {"available": False, "healthy": False}


def test_tenant_minute_freshness_forwards_with_read_credentials_without_admin_permission(
    env, minute_platform, monkeypatch,
):
    minute_platform.state.minute_refresh = SimpleNamespace(status=lambda: {
        "healthy": True,
        "last_error": "private collector diagnostic",
    })
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.ASGITransport(app=minute_platform), **kwargs,
    ))
    reader = PlatformAdmin(
        "http://platform", "test-platform-read-token", allowed=market_read_path,
    )
    env["client"].app.state.platform_reads = reader
    token, account = register(env)
    env["client"].cookies.set("tf_session", token)
    try:
        assert account["permissions"] == ["account:manage", "business:read", "business:write"]
        response = env["client"].get(STATUS_PATH)
        assert response.status_code == 200, response.text
        assert response.json() == {"available": True, "healthy": True}
        assert response.headers["cache-control"] == "no-store"
        for path in ("/api/settings/minute-refresh/status", "/api/data/status"):
            response = env["client"].get(path)
            assert response.status_code == 403
        assert env["client"].post(STATUS_PATH).status_code != 200
        env["client"].cookies.clear()
        assert env["client"].get(STATUS_PATH).status_code == 401
    finally:
        asyncio.run(reader.close())
