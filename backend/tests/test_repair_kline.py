import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline
from app.data_providers.base import ProviderCapabilities
from app.services import kline_periods, pipeline_jobs, preferences, repair_daily
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 26)
SYMBOLS = ["600000.SH", "000001.SZ", "300001.SZ", "688001.SH"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "jobs"))
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(repair_daily, "cn_today", lambda: DAY)
    monkeypatch.setattr(kline, "cn_today", lambda: DAY)
    path = tmp_path / "instruments/instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": SYMBOLS, "name": ["沪", "深", "创", "科"]}).write_parquet(path)
    app = FastAPI()
    app.state.repo = KlineRepository(DataStore(tmp_path))
    app.state.capabilities = SimpleNamespace(has=lambda _: True)
    app.include_router(kline.router)
    yield app
    app.state.repo.db.close()
    preferences._invalidate_cache()


def minute_frame():
    return pl.DataFrame({
        "symbol": [SYMBOLS[0]] * 2,
        "datetime": [datetime(2026, 10, 26, 9, 31), datetime(2026, 10, 26, 9, 40)],
        "open": [10., 11.], "high": [12., 13.], "low": [9., 10.], "close": [11., 12.],
        "volume": [100., 200.], "amount": [1000., 2000.],
    })


@pytest.mark.parametrize("period", ["1m", "5m", "15m", "1w"])
def test_repair_fetches_requested_period_and_upserts_its_own_table(app, monkeypatch, period):
    calls = []
    frame = minute_frame()
    if period == "1w":
        frame = frame.with_columns(pl.col("datetime").dt.date().alias("date")).drop("datetime")

    def fetch(symbols, start, end, *, asset_type, freq):
        calls.append((symbols, start, end, asset_type, freq))
        return frame

    provider = SimpleNamespace(
        capabilities=ProviderCapabilities(daily=True, minute=True), get_klines=fetch,
    )
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: provider)
    catalog = (app.state.repo.store.data_dir / "instruments/instruments.parquet").read_bytes()
    for _ in range(2):
        result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period=period)
        assert result["rows"] == (1 if period == "1w" else 2)
        assert result["period"] == period
    assert calls == [(sorted(SYMBOLS), datetime.combine(DAY, datetime.min.time()),
                      datetime.combine(DAY, datetime.max.time()), "stock", period)] * 2
    assert app.state.repo.execute_one(f"SELECT count(*) FROM {kline_periods.TABLES[period]}")[0] == result["rows"]
    assert (app.state.repo.store.data_dir / "instruments/instruments.parquet").read_bytes() == catalog


def test_ten_minute_repair_aggregates_local_minutes_without_provider(app, monkeypatch):
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: pytest.fail("10m must stay local"))
    kline_periods.persist_period(app.state.repo, "1m", minute_frame())
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period="10m")
    assert result["source"] == "1m"
    assert result["rows"] == 1
    assert app.state.repo.execute_one(
        "SELECT open, high, low, close, volume, amount FROM kline_10minute",
    ) == (10., 13., 9., 12., 300., 3000.)


def test_ten_minute_repair_without_base_data_explains_the_missing_dependency(app):
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period="10m")
    assert "1分K" in result["error"]
    assert not any((app.state.repo.store.data_dir / "kline_10minute").rglob("*.parquet"))


def test_weekly_repair_includes_the_week_containing_the_selected_start_date(app, monkeypatch):
    starts = []
    def fetch(symbols, start, end, **kwargs):
        starts.append(start)
        return pl.DataFrame({
            "symbol": [SYMBOLS[0]], "date": [date(2026, 10, 19)],
            "open": [10.], "high": [13.], "low": [9.], "close": [12.],
            "volume": [300.], "amount": [3000.],
        })
    provider = SimpleNamespace(capabilities=ProviderCapabilities(daily=True), get_klines=fetch)
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: provider)
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, date(2026, 10, 21), period="1w")
    assert result["rows"] == 1
    assert starts == [datetime(2026, 10, 19)]
    assert app.state.repo.execute_one("SELECT date FROM kline_weekly")[0] == date(2026, 10, 19)


def test_daily_repair_keeps_the_original_pipeline_and_cache_refresh(app, monkeypatch):
    calls = []
    monkeypatch.setattr("app.jobs.daily_pipeline.run_now", lambda *args, **kwargs: calls.append(kwargs) or {"daily_rows": 10})
    monkeypatch.setattr(app.state.repo, "refresh_cache", lambda: calls.append("refresh"))
    assert repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY) == {"daily_rows": 10}
    assert calls == [{"on_progress": None, "override_start_date": DAY}, "refresh"]


@pytest.mark.asyncio
async def test_repair_api_keeps_selected_period_and_reports_real_completion(app):
    kline_periods.persist_period(app.state.repo, "1m", minute_frame())
    app.state.capabilities = SimpleNamespace(has=lambda _: False)
    result = await kline.repair_daily(SimpleNamespace(
        app=app, json=lambda: async_body({"start_date": DAY.isoformat(), "period": "10m"}),
    ))
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
    job = pipeline_jobs.job_store.get(result["job_id"])
    assert job["status"] == "succeeded", job
    assert job["request"] == {"start_date": DAY.isoformat(), "period": "10m"}
    assert job["result"]["rows"] == 1
    assert job["result"]["period"] == "10m"
    assert job["stage_pct"] == job["progress"] == 100


async def async_body(body):
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "5m", "15m", "1d", "1w"])
async def test_repair_api_uses_custom_route_without_tickflow_entitlement(app, monkeypatch, period):
    app.state.capabilities = SimpleNamespace(has=lambda _: False)
    calls = []
    frame = minute_frame()
    if period in ("1d", "1w"):
        frame = frame.with_columns(pl.col("datetime").dt.date().alias("date")).drop("datetime")

    def fetch(symbols, start, end, *, asset_type, freq):
        calls.append(freq)
        return frame

    provider = SimpleNamespace(capabilities=ProviderCapabilities(daily=True, minute=True), get_klines=fetch)
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: provider)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "custom")
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "custom")
    if period == "1d":
        monkeypatch.setattr("app.jobs.daily_pipeline.run_now", lambda *a, **kw: {"daily_rows": 2})
    result = await kline.repair_daily(SimpleNamespace(
        app=app, json=lambda: async_body({"start_date": DAY.isoformat(), "period": period}),
    ))
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
    job = pipeline_jobs.job_store.get(result["job_id"])
    assert job["status"] == "succeeded", job
    assert job["request"]["period"] == period
    assert calls == ([] if period == "1d" else [period])
    if period != "1d":
        assert job["result"]["period"] == period
        assert job["result"]["rows"] == (1 if period == "1w" else 2)


@pytest.mark.asyncio
async def test_repair_cancel_after_fetch_stops_before_writing_and_releases_slot(app, monkeypatch):
    def fetch(*args, **kwargs):
        pipeline_jobs.job_store.terminate(pipeline_jobs.job_store.active_id(), "用户取消")
        return minute_frame()

    provider = SimpleNamespace(capabilities=ProviderCapabilities(minute=True), get_klines=fetch)
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: provider)
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "custom")
    result = await kline.repair_daily(SimpleNamespace(
        app=app, json=lambda: async_body({"start_date": DAY.isoformat(), "period": "5m"}),
    ))
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running", "cancelling"}:
            await asyncio.sleep(0.01)
    assert pipeline_jobs.job_store.get(result["job_id"])["status"] == "cancelled"
    assert not any((app.state.repo.store.data_dir / "kline_5minute").rglob("*.parquet"))
    assert pipeline_jobs._run_slot_owner is None


@pytest.mark.parametrize("period", ["1m", "1d", "1w"])
def test_repair_api_checks_selected_tickflow_entitlement(app, monkeypatch, period):
    app.state.capabilities = SimpleNamespace(has=lambda _: False)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tickflow")
    with TestClient(app) as client:
        response = client.post("/api/kline/repair_daily", json={"start_date": DAY.isoformat(), "period": period})
    assert response.status_code == 403
    assert pipeline_jobs.job_store.list_recent() == []


@pytest.mark.parametrize("period", ["invalid", "../1m", None, []])
def test_repair_api_rejects_invalid_periods_before_creating_jobs(app, period):
    with TestClient(app) as client:
        response = client.post("/api/kline/repair_daily", json={"start_date": DAY.isoformat(), "period": period})
    assert response.status_code == 422
    assert pipeline_jobs.job_store.list_recent() == []
