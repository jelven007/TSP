from datetime import date

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.instruments import router
from app.instrument_history import load_instrument_history
from app.services import instrument_sync
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def client(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: date(2026, 10, 24))
    monkeypatch.setattr("app.services.instrument_catalog.cn_today", lambda: date(2026, 10, 24))
    monkeypatch.setattr("app.services.data_release.publish_data_release", lambda *a, **kw: {"release_id": "test"})
    app = FastAPI()
    app.state.repo = repo
    @app.middleware("http")
    async def account(request, call_next):
        if request.headers.get("x-test-role"):
            request.state.account = {"role": request.headers["x-test-role"]}
        return await call_next(request)
    app.include_router(router)
    with TestClient(app, headers={"x-test-role": "admin"}) as client:
        yield client, repo, tmp_path
    repo.db.close()


def test_admin_crud_search_validation_and_same_day_deletion(client):
    api, repo, directory = client
    body = {"symbol": "600000.SH", "name": "浦发银行", "listing_date": "1999-11-10",
            "total_shares": 1000, "float_shares": 800}
    assert api.get("/api/data/instruments").json()["total"] == 0
    response = api.post("/api/data/instruments", json=body)
    assert response.status_code == 201, response.text
    assert repo.get_name_map()["600000.SH"] == "浦发银行"
    assert api.post("/api/data/instruments", json=body).status_code == 409
    assert api.post("/api/data/instruments", json={**body, "symbol": "510300.SH"}).status_code == 422
    assert api.get("/api/data/instruments?q=银行&exchange=SH").json()["total"] == 1
    assert api.get("/api/data/instruments?exchange=SZ").json()["total"] == 0
    assert api.get("/api/data/instruments?page_size=201").status_code == 422
    body.pop("symbol")
    body["name"] = "*ST示例"
    assert api.put("/api/data/instruments/600000.SH", json=body).status_code == 200
    assert repo.get_name_map()["600000.SH"] == "*ST示例"
    assert api.put("/api/data/instruments/600000.SH", json={**body, "float_shares": 2000}).status_code == 422
    assert api.delete("/api/data/instruments/600000.SH").status_code == 200
    assert api.get("/api/data/instruments").json()["items"] == []
    assert repo.get_instruments().is_empty()
    history = load_instrument_history(directory)
    assert history.height == 1
    assert history["name"][0] == "*ST示例"
    assert history["is_listed"][0] is False
    assert api.delete("/api/data/instruments/600000.SH").status_code == 404


@pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
def test_non_admin_cannot_access_any_catalog_operation(client, method):
    api, _, _ = client
    url = "/api/data/instruments" if method in {"get", "post"} else "/api/data/instruments/600000.SH"
    kwargs = {"json": {"symbol": "600000.SH", "name": "测试"}} if method == "post" else (
        {"json": {"name": "测试"}} if method == "put" else {}
    )
    assert getattr(api, method)(url, headers={"x-test-role": "member"}, **kwargs).status_code == 403


def test_official_failure_preserves_both_snapshots_and_manual_metadata(client, monkeypatch):
    api, _, directory = client
    api.post("/api/data/instruments", json={"symbol": "600000.SH", "name": "测试", "total_shares": 1000})
    target = directory / "instruments/instruments.parquet"
    history = directory / "instrument_status/history.parquet"
    before = target.read_bytes(), history.read_bytes()
    from app.data_providers.official_stock_catalog import OfficialCatalogError
    def fail():
        raise OfficialCatalogError("断网")
    monkeypatch.setattr("app.data_providers.official_stock_catalog.fetch_official_stock_catalog", fail)
    assert instrument_sync.sync_instruments(directory) == 0
    assert (target.read_bytes(), history.read_bytes()) == before
    monkeypatch.setattr("app.data_providers.official_stock_catalog.fetch_official_stock_catalog",
                        lambda: [{"symbol": "600000.SH", "name": "官方名称"}])
    assert instrument_sync.sync_instruments(directory) == 1
    row = pl.read_parquet(target).row(0, named=True)
    assert row["name"] == "官方名称"
    assert row["total_shares"] == 1000
    assert row["total_shares_source"] == "manual"


def test_snapshot_write_failure_restores_status_history(client, monkeypatch):
    api, _, directory = client
    api.post("/api/data/instruments", json={"symbol": "600000.SH", "name": "测试"})
    before = load_instrument_history(directory)
    original = instrument_sync.atomic_write_parquet
    def write(frame, path):
        if path.parent.name == "instruments":
            raise OSError("disk full")
        original(frame, path)
    monkeypatch.setattr(instrument_sync, "atomic_write_parquet", write)
    with pytest.raises(OSError, match="disk full"):
        api.delete("/api/data/instruments/600000.SH")
    assert load_instrument_history(directory).equals(before)
    assert pl.read_parquet(directory / "instruments/instruments.parquet")["name"][0] == "测试"


def test_catalog_job_is_independent_of_an_active_price_job(tmp_path):
    from app.services.pipeline_jobs import JobStore
    store = JobStore(store_dir=tmp_path)
    price, _ = store.create(kind="daily_pipeline")
    catalog, created = store.create(kind="instruments_sync")
    assert created and catalog != price
    assert store.active_id() == price
    assert store.create(kind="instruments_sync") == (catalog, False)
    store.start(catalog)
    store.succeed(catalog, {})
    assert store.active_id() == price


def test_catalog_shutdown_waits_for_a_cancelled_awaiters_worker(tmp_path, monkeypatch):
    import asyncio
    import threading

    from app.jobs import daily_pipeline
    from app.services import pipeline_jobs
    store = pipeline_jobs.JobStore(store_dir=tmp_path)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def sync(repo):
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        return {"instruments_refreshed": True}

    monkeypatch.setattr(daily_pipeline, "run_instruments_sync", sync)

    async def exercise():
        task = asyncio.create_task(daily_pipeline.run_catalog_job(object(), source="startup"))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            waiting = asyncio.create_task(daily_pipeline.stop_catalog_jobs())
            await asyncio.sleep(0)
            assert not waiting.done()
            release.set()
            await asyncio.wait_for(waiting, timeout=3)
            assert finished.is_set()
        finally:
            release.set()
            await daily_pipeline.stop_catalog_jobs()

    asyncio.run(exercise())
    assert store.list_recent()[0]["status"] == "succeeded"


def test_catalog_cancel_before_download_has_a_terminal_record(tmp_path, monkeypatch):
    import asyncio

    from app.jobs import daily_pipeline
    from app.services import pipeline_jobs

    store = pipeline_jobs.JobStore(store_dir=tmp_path)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    original = store.start

    def start(job_id):
        original(job_id)
        pipeline_jobs.request_cancel(job_id)
    monkeypatch.setattr(store, "start", start)
    monkeypatch.setattr(daily_pipeline, "run_instruments_sync", lambda _: pytest.fail("cancelled before download"))
    asyncio.run(daily_pipeline.run_catalog_job(object(), source="scheduled"))
    assert store.list_recent()[0]["status"] == "cancelled"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_multitenant_catalog_requires_permission_before_forwarding(method):
    from types import SimpleNamespace

    from fastapi.responses import JSONResponse

    from app.multitenancy.access import middleware
    from app.multitenancy.platform import management_path, market_read_path

    path = "/api/data/instruments" if method in {"GET", "POST"} else "/api/data/instruments/600000.SH"
    assert management_path(method, path)
    assert not market_read_path(method, path)
    app = FastAPI()
    app.middleware("http")(middleware)
    account = {
        "id": "admin", "role": "admin", "default_tenant_id": None,
        "auth_version": 1, "permissions": ["data:manage"],
    }
    app.state.account_identity = SimpleNamespace(current_user=lambda _: account)
    seen = []

    class Forward:
        async def forward(self, request):
            seen.append((request.method, request.url.path, request.state.account["role"]))
            return JSONResponse({"forwarded": True})
    app.state.platform_admin = Forward()

    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        response = client.request(method, path, json={"name": "测试"} if method in {"POST", "PUT"} else None)
        assert response.status_code == 200
        assert response.json() == {"forwarded": True}
        assert response.headers["cache-control"] == "no-store"
        account.update(role="tenant", permissions=["business:read", "business:write"])
        assert client.request(method, path).status_code == 403
    assert seen == [(method, path, "admin")]
