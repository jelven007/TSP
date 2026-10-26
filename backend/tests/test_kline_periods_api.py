import asyncio
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import data, kline, settings
from app.services import kline_periods, pipeline_jobs, preferences
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    monkeypatch.setattr(kline, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "jobs"), raising=False)
    monkeypatch.setattr(pipeline_jobs, "job_store", kline.job_store)
    app = FastAPI()
    app.state.repo = KlineRepository(DataStore(tmp_path))
    app.state.capabilities = SimpleNamespace(has=lambda cap: True)
    app.state.indicators_ready = True
    app.include_router(settings.router)
    app.include_router(kline.router)
    app.include_router(data.router)
    yield app
    app.state.repo.db.close()


def test_period_config_validation_persistence_and_empty_schema(app):
    with TestClient(app) as client:
        configs = client.get("/api/settings/kline-sync").json()
        assert configs["15m"]["interval_seconds"] == 900
        config = {**configs["1w"], "enabled": False, "time": "16:30"}
        assert client.post("/api/settings/kline-sync/1w", json=config).status_code == 200
        assert client.get("/api/settings/kline-sync").json()["1w"] == config
        assert client.post("/api/settings/kline-sync/unknown", json=config).status_code == 422
        assert client.post("/api/settings/kline-sync/1w", json={**config, "time": "12:00"}).status_code == 422
        for table in ("5m", "10m", "15m", "weekly"):
            fields = client.get(f"/api/data/schema/{table}").json()
            assert {row["name"] for row in fields} >= {"symbol", "open", "high", "low", "close", "volume", "amount"}
        assert client.post("/api/kline/sync_period", json={"period": "../1m"}).status_code == 422
        assert client.post("/api/kline/sync_period", json={"period": "5m", "value": 0}).status_code == 422


@pytest.mark.asyncio
async def test_manual_ten_minute_job_has_real_rows_and_completion(app, monkeypatch):
    repo = app.state.repo
    now = datetime(2026, 10, 26, 9, 31)
    frame = pl.DataFrame({
        "symbol": ["600000.SH"], "datetime": [now],
        "open": [10.], "high": [11.], "low": [9.], "close": [10.5],
        "volume": [100.], "amount": [1000.],
    })
    kline_periods.persist_period(repo, "1m", frame)
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": ["600000.SH"]}))
    monkeypatch.setattr("app.market_time.cn_today", lambda: now.date())
    request = SimpleNamespace(app=app)
    result = await kline.sync_period_job(request, {"period": "10m", "value": 1, "unit": "month"})
    for _ in range(50):
        job = pipeline_jobs.job_store.get(result["job_id"])
        if job["status"] not in {"queued", "running"}:
            break
        await asyncio.sleep(0.01)
    assert job["status"] == "succeeded"
    assert job["result"]["rows"] == 1
    assert repo.execute_one("SELECT close FROM kline_10minute")[0] == 10.5


@pytest.mark.parametrize(("endpoint", "period", "column", "timestamp"), [
    ("minute", "5m", "datetime", datetime(2026, 10, 23, 9, 35)),
    ("minute", "15m", "datetime", datetime(2026, 10, 23, 9, 45)),
    ("daily", "1w", "date", datetime(2026, 10, 23).date()),
])
def test_existing_query_endpoints_return_requested_period(app, monkeypatch, endpoint, period, column, timestamp):
    repo = app.state.repo
    frame = pl.DataFrame({"symbol": ["600000.SH"], column: [timestamp],
                          "open": [10.], "high": [11.], "low": [9.], "close": [10.5],
                          "volume": [100.], "amount": [1000.]})
    kline_periods.persist_period(repo, period, frame)
    monkeypatch.setattr(kline, "_get_stock_info", lambda repo, symbol: {"name": "浦发银行"})
    monkeypatch.setattr(kline.kline_sync, "fetch_minute_single", lambda *a, **kw: pl.DataFrame())
    monkeypatch.setattr(kline.kline_sync, "sync_daily_batch", lambda *a, **kw: pl.DataFrame())
    params = {"symbol": "600000.SH", "freq": period}
    if endpoint == "minute":
        params["date"] = "2026-10-23"
    else:
        params.update(start_date="2026-10-19", end_date="2026-10-23")
    with TestClient(app) as client:
        response = client.get(f"/api/kline/{endpoint}", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "local"
    assert len(response.json()["rows"]) == 1
    assert response.json()["rows"][0]["close"] == 10.5


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["5m", "15m", "1w"])
async def test_offline_job_uses_existing_provider_period_and_updates_status(app, monkeypatch, period):
    from app.data_providers.base import ProviderCapabilities
    calls = []
    column = "date" if period == "1w" else "datetime"
    now = datetime(2026, 10, 23, 9, 45)
    def fetch(symbols, start, end, *, asset_type, freq):
        calls.append(freq)
        stamp = now.date() if column == "date" else now
        return pl.DataFrame({"symbol": ["600000.SH"], column: [stamp], "open": [10.], "high": [11.], "low": [9.], "close": [10.5], "volume": [100.], "amount": [1000.]})
    provider = SimpleNamespace(capabilities=ProviderCapabilities(minute=True, daily=True), get_klines=fetch)
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)
    monkeypatch.setattr(app.state.repo, "get_instruments", lambda: pl.DataFrame({"symbol": ["600000.SH"]}))
    monkeypatch.setattr("app.market_time.cn_today", lambda: now.date())
    result = await kline.sync_period_job(SimpleNamespace(app=app), {"period": period, "value": 1, "unit": "month"})
    async with asyncio.timeout(3):
        while True:
            job = pipeline_jobs.job_store.get(result["job_id"])
            if job["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.01)
    assert job["status"] == "succeeded", job
    assert calls and set(calls) == {period}
    assert job["result"]["rows"] == 1
    stats = data._safe_aggregate_minute(app.state.repo, kline_periods.TABLES[period])
    assert stats["rows"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "5m", "10m", "15m", "1w"])
async def test_offline_job_preserves_service_total_and_stage_progress(app, monkeypatch, period):
    events = []
    store = pipeline_jobs.job_store
    original_progress = store.progress
    def progress(job_id, stage, pct, msg, **kwargs):
        original_progress(job_id, stage, pct, msg, **kwargs)
        events.append(store.get(job_id))
    monkeypatch.setattr(store, "progress", progress)
    def sync(repo, freq, start, end, *, on_progress):
        on_progress(f"sync_{freq}", 38, "已写入第一批", stage_pct=33)
        on_progress(f"sync_{freq}", 95, "已写入全部批次", stage_pct=100)
        return {"period": freq, "rows": 10, "universe_size": 1}
    monkeypatch.setattr(kline_periods, "sync_period", sync)
    result = await kline.sync_period_job(SimpleNamespace(app=app), {"period": period, "value": 1, "unit": "month"})
    async with asyncio.timeout(3):
        while store.get(result["job_id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
    assert store.get(result["job_id"])["status"] == "succeeded"
    assert [(event["progress"], event["stage_pct"]) for event in events[-4:]] == [
        (0, 0), (38, 33), (95, 100), (100, 100),
    ]


def test_empty_daily_history_uses_calendar_range_and_retains_pipeline(app, monkeypatch):
    from app.services import extend_history
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
    monkeypatch.setattr(extend_history, "_resolve_universe", lambda caps: ["600000.SH"])
    calls = []
    def daily(symbols, repo, caps, **kwargs):
        calls.append(("daily", kwargs["start_date"], kwargs["end_date"]))
        return 10
    monkeypatch.setattr(extend_history.kline_sync, "sync_and_persist_daily_batch", daily)
    monkeypatch.setattr(extend_history.kline_sync, "sync_adj_factor", lambda *a, **kw: calls.append(("adj",)) or (1, []))
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", lambda: calls.append(("enriched",)) or 10)
    monkeypatch.setattr("app.market_time.cn_today", lambda: datetime(2024, 3, 31).date())
    caps = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(), Cap.ADJ_FACTOR: CapabilityLimits()})
    result = extend_history.run_extend_history(app.state.repo, caps, 1, "month")
    assert "error" not in result
    assert calls[0] == ("daily", datetime(2024, 2, 29), datetime(2024, 3, 31, 23, 59, 59, 999999))
    assert [call[0] for call in calls] == ["daily", "adj", "enriched"]


@pytest.mark.parametrize(("endpoint", "period"), [("minute", "5m"), ("minute", "15m"), ("daily", "1w")])
def test_empty_query_fetches_requested_remote_period_and_propagates_failure(app, monkeypatch, endpoint, period):
    from app.data_providers.base import ProviderCapabilities
    calls = []
    def fetch(symbols, start, end, *, asset_type, freq):
        calls.append((freq, start, end))
        column = "date" if freq == "1w" else "datetime"
        stamp = datetime(2026, 10, 23).date() if column == "date" else datetime(2026, 10, 23, 9, 45)
        return pl.DataFrame({"symbol": symbols, column: [stamp], "open": [10.], "high": [11.], "low": [9.], "close": [10.5], "volume": [100.], "amount": [1000.]})
    provider = SimpleNamespace(capabilities=ProviderCapabilities(minute=True, daily=True), get_klines=fetch)
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)
    monkeypatch.setattr(kline, "_get_stock_info", lambda *a: {"name": "测试"})
    params = {"symbol": "600000.SH", "freq": period, "date": "2026-10-23",
              "start_date": "2026-10-21", "end_date": "2026-10-23"}
    with TestClient(app) as client:
        response = client.get(f"/api/kline/{endpoint}", params=params)
        assert response.status_code == 200, response.text
        assert response.json()["source"] == "live"
        assert response.json()["rows"][0]["close"] == 10.5
        assert calls[-1][0] == period
        if period == "1w":
            assert response.json()["rows"][0]["date"] == "2026-10-19"
        assert client.get(f"/api/kline/{endpoint}", params={**params, "freq": "invalid"}).status_code == 422
        def fail(*a, **kw):
            raise RuntimeError("provider offline")
        provider.get_klines = fail
        response = client.get(f"/api/kline/{endpoint}", params=params)
        assert response.status_code == 502
