import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.routes import router
from app.cell.database import Databases
from app.multitenancy.platform import PlatformAdmin, market_read_path
from app.services.observability import (
    measure_runtime_lock,
    measure_runtime_stage,
    reset_runtime_metrics,
    runtime_metrics_snapshot,
)


@pytest.fixture(autouse=True)
def reset_metrics():
    reset_runtime_metrics()
    yield
    reset_runtime_metrics()


def test_runtime_failure_releases_lock_and_separates_wait_from_execution():
    lock = threading.Lock()
    with pytest.raises(RuntimeError, match="failed"), measure_runtime_lock(lock, "public.refresh"):
        raise RuntimeError("failed")
    assert lock.acquire(blocking=False)
    lock.release()
    metrics = runtime_metrics_snapshot()
    assert metrics["public.refresh"]["calls"] == 1
    assert metrics["public.refresh"]["failures"] == 1
    assert metrics["public.refresh_wait"]["failures"] == 0
    assert metrics["public.refresh"]["duration_ms"]["p99"] is not None
    with pytest.raises(ValueError), measure_runtime_stage("tenant-sensitive-user-id"):
        pass
    assert len(runtime_metrics_snapshot()) == 2


def test_http_serialization_keeps_runtime_stages_and_sanitized_pool_stats():
    with measure_runtime_stage("tenant.factor_definitions"):
        pass
    pool = SimpleNamespace(get_stats=lambda: {
        "pool_size": 4, "requests_wait_ms": 18, "requests_waiting": 2,
        "dsn": "must-never-appear", "unknown_metric": 7,
    })
    db = object.__new__(Databases)
    db.control = db.data = pool
    app = FastAPI()
    app.include_router(router)
    app.state.account_identity = SimpleNamespace(db=db)
    response = TestClient(app).get("/api/observability")
    assert response.status_code == 200
    payload = response.json()
    assert payload["stages"]["tenant.factor_definitions"]["calls"] == 1
    assert payload["pools"]["business"] == {
        "pool_size": 4, "requests_wait_ms": 18, "requests_waiting": 2,
    }
    assert "must-never-appear" not in response.text
    assert "unknown_metric" not in response.text


@pytest.mark.parametrize("allowed,path", [
    (market_read_path, "/api/kline/daily"),
    (None, "/api/settings"),
])
def test_platform_forwarding_propagates_correlation_and_keeps_identity_local(allowed, path):
    async def verify():
        seen = []

        async def remote(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        gateway = PlatformAdmin("https://platform.test", token="internal-only",
                                **({"allowed": allowed} if allowed else {}))
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(
            base_url="https://platform.test",
            headers={"Authorization": "Bearer internal-only"},
            transport=httpx.MockTransport(remote),
        )
        request = Request({
            "type": "http", "method": "GET", "scheme": "https",
            "server": ("api.test", 443), "path": path, "query_string": b"",
            "headers": [(b"cookie", b"private-cookie"), (b"authorization", b"Bearer private-token"),
                        (b"x-tenant-id", b"private-tenant"), (b"x-request-id", b"untrusted-raw")],
            "state": {"request_id": "api-request-42", "account": {"email": "admin@test.local"}},
        })
        response = await gateway.forward(request)
        assert response.status_code == 200
        assert seen[0].headers["x-request-id"] == "api-request-42"
        assert seen[0].headers["authorization"] == "Bearer internal-only"
        assert "cookie" not in seen[0].headers and "x-tenant-id" not in seen[0].headers
        await gateway.close()

    asyncio.run(verify())
