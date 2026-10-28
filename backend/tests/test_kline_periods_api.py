import asyncio
from datetime import date, datetime
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
@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
async def test_manual_derived_minute_job_has_real_rows_and_completion(app, monkeypatch, period):
    repo = app.state.repo
    now = datetime(2026, 10, 26, 9, 31)
    frame = pl.DataFrame({
        "symbol": ["600000.SH"], "datetime": [now],
        "open": [10.], "high": [11.], "low": [9.], "close": [10.5],
        "volume": [100.], "amount": [1000.],
    })
    kline_periods.persist_period(repo, "1m", frame)
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("offline must stay local"))
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": ["600000.SH"]}))
    monkeypatch.setattr("app.market_time.cn_today", lambda: now.date())
    request = SimpleNamespace(app=app)
    result = await kline.sync_period_job(request, {"period": period, "value": 1, "unit": "month"})
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
    job = pipeline_jobs.job_store.get(result["job_id"])
    assert job["status"] == "succeeded"
    assert job["result"]["rows"] == 1
    assert job["result"]["source"] == "1m"
    assert repo.execute_one(f"SELECT close FROM {kline_periods.TABLES[period]}")[0] == 10.5


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
async def test_empty_derived_offline_job_fails_with_local_dependency(app, monkeypatch, period):
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("empty base must stay local"))
    monkeypatch.setattr(app.state.repo, "get_instruments", lambda: pl.DataFrame({"symbol": ["600000.SH"]}))
    result = await kline.sync_period_job(SimpleNamespace(app=app), {"period": period, "value": 1, "unit": "month"})
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
    job = pipeline_jobs.job_store.get(result["job_id"])
    assert job["status"] == "failed", job
    assert "1分K" in job["error"]


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
@pytest.mark.parametrize("period", ["1m"])
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
async def test_offline_weekly_job_aggregates_local_daily_without_provider(app, monkeypatch):
    repo = app.state.repo
    repo.append_daily(pl.DataFrame({
        "symbol": ["600000.SH"] * 3,
        "date": [datetime(2026, 10, day).date() for day in (21, 22, 23)],
        "open": [10., 11., 12.], "high": [12., 13., 14.], "low": [9., 10., 11.],
        "close": [11., 12., 13.], "volume": [100., 200., 300.], "amount": [1000., 2000., 3000.],
    }))
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("weekly offline must stay local"))
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": ["600000.SH"]}))
    monkeypatch.setattr("app.market_time.cn_today", lambda: datetime(2026, 10, 23).date())

    result = await kline.sync_period_job(SimpleNamespace(app=app), {"period": "1w", "value": 1, "unit": "month"})
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
    job = pipeline_jobs.job_store.get(result["job_id"])

    assert job["status"] == "succeeded", job
    assert job["result"]["source"] == "1d"
    assert job["result"]["rows"] == 1
    assert repo.execute_one("SELECT date, close FROM kline_weekly") == (datetime(2026, 10, 19).date(), 13.)


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


def test_weekly_query_aggregates_local_daily_without_remote_fallback(app, monkeypatch):
    app.state.repo.append_daily(pl.DataFrame({
        "symbol": ["600000.SH"] * 3,
        "date": [datetime(2026, 10, day).date() for day in (21, 22, 23)],
        "open": [10., 11., 12.], "high": [12., 13., 14.], "low": [9., 10., 11.],
        "close": [11., 12., 13.], "volume": [100., 200., 300.], "amount": [1000., 2000., 3000.],
    }))
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("weekly query must stay local"))
    monkeypatch.setattr(kline, "_get_stock_info", lambda *a: {"name": "测试"})
    params = {"symbol": "600000.SH", "freq": "1w", "date": "2026-10-23",
              "start_date": "2026-10-21", "end_date": "2026-10-23"}
    with TestClient(app) as client:
        response = client.get("/api/kline/daily", params=params)
        assert response.status_code == 200, response.text
        assert response.json()["source"] == "local"
        assert response.json()["rows"][0]["close"] == 13.
        assert response.json()["rows"][0]["date"] == "2026-10-19"
        assert client.get("/api/kline/daily", params={**params, "freq": "invalid"}).status_code == 422


async def wait_for_job(job_id):
    async with asyncio.timeout(10):
        while pipeline_jobs.job_store.get(job_id)["status"] in {"queued", "running", "cancelling"}:
            await asyncio.sleep(0.01)
    return pipeline_jobs.job_store.get(job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("period", "mode"), [("1m", "full"), ("1m", "missing"), ("1d", "full"), ("1d", "missing")])
async def test_base_period_sync_forwards_explicit_closed_range_to_shared_repair_service(
    app, monkeypatch, period, mode,
):
    from app.services import repair_daily

    calls = []
    monkeypatch.setattr(kline_periods, "_period_provider", lambda _: (object(), "rustdx"))

    def run(repo, caps, start_date, **kwargs):
        calls.append((repo, caps, start_date, kwargs["end_date"], kwargs["period"], kwargs["mode"]))
        return {"period": period, "mode": mode, "rows": 1}

    monkeypatch.setattr(repair_daily, "run_repair_daily", run)
    body = {
        "period": period,
        "start_date": "2026-10-21",
        "end_date": "2026-10-23",
        "mode": mode,
    }
    started = await kline.sync_period_job(SimpleNamespace(app=app), body)
    job = await wait_for_job(started["job_id"])

    assert job["status"] == "succeeded", job
    assert job["request"] == body
    assert calls == [(
        app.state.repo,
        app.state.capabilities,
        date(2026, 10, 21),
        date(2026, 10, 23),
        period,
        mode,
    )]


@pytest.mark.asyncio
async def test_missing_sync_defers_provider_resolution_until_after_local_gap_scan(app, monkeypatch):
    from app.services import repair_daily

    monkeypatch.setattr(
        kline_periods,
        "_period_provider",
        lambda _: pytest.fail("no-gap missing sync must not resolve the upstream provider"),
    )
    monkeypatch.setattr(
        repair_daily,
        "run_repair_daily",
        lambda *args, **kwargs: {
            "period": "1m",
            "mode": "missing",
            "rows": 0,
            "missing_before": 0,
            "remaining": 0,
            "requests": 0,
            "message": "选定区间没有遗漏数据, 无需获取",
        },
    )
    body = {
        "period": "1m",
        "start_date": "2026-10-21",
        "end_date": "2026-10-23",
        "mode": "missing",
    }

    started = await kline.sync_period_job(SimpleNamespace(app=app), body)
    job = await wait_for_job(started["job_id"])

    assert job["status"] == "succeeded", job
    assert job["result"]["requests"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"period": "1m", "start_date": "2026-10-23", "end_date": "2026-10-21", "mode": "full"},
        {"period": "1d", "start_date": "2026-10-21", "end_date": "2026-10-29", "mode": "missing"},
        {"period": "1d", "start_date": "2026-10-21", "end_date": "2026-10-23", "mode": "invalid"},
    ],
)
def test_base_period_sync_rejects_invalid_ranges_and_modes_before_creating_jobs(app, body, monkeypatch):
    monkeypatch.setattr(kline, "cn_today", lambda: date(2026, 10, 28))
    with TestClient(app) as client:
        response = client.post("/api/kline/sync_period", json=body)
    assert response.status_code in {400, 422}
    assert pipeline_jobs.job_store.list_recent() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["5m", "10m", "15m", "1w"])
async def test_generation_covers_existing_base_range_and_can_regenerate(app, monkeypatch, period):
    repo = app.state.repo
    base = "1d" if period == "1w" else "1m"
    dates = [date(2026, 6, 1), date(2026, 10, 21), date(2026, 10, 26)]
    column = "date" if base == "1d" else "datetime"
    frame = pl.DataFrame({
        "symbol": ["600000.SH"] * 3,
        column: dates if base == "1d" else [datetime.combine(day, datetime.min.time()).replace(hour=9, minute=31) for day in dates],
        "open": [10.] * 3, "high": [11.] * 3, "low": [9.] * 3,
        "close": [10.5] * 3, "volume": [100.] * 3, "amount": [1000.] * 3,
    })
    kline_periods.persist_period(repo, base, frame)
    existing = frame.slice(1, 1)
    existing = kline_periods.aggregate_weekly(existing) if period == "1w" else kline_periods.aggregate_minute_period(existing, period)
    kline_periods.persist_period(repo, period, existing)
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": ["600000.SH"]}))
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("generation must stay local"))
    monkeypatch.setattr(app.state.capabilities, "has", lambda cap: False)
    for expected_close in (10.5, 12.5):
        if expected_close == 12.5:
            kline_periods.persist_period(repo, base, frame.tail(1).with_columns(pl.lit(12.5).alias("close")))
        started = await kline.sync_period_job(SimpleNamespace(app=app), {"period": period})
        job = await wait_for_job(started["job_id"])
        assert job["status"] == "succeeded", job
        assert job["result"]["rows"] == 3
        assert job["result"]["source"] == base
        assert job["result"]["start"].startswith("2026-06-01")
        assert job["result"]["end"].startswith("2026-10-26")
        assert repo.execute_one(f"SELECT count(*) FROM {kline_periods.TABLES[period]}")[0] == 3
        assert repo.execute_one(f"SELECT close FROM {kline_periods.TABLES[period]} ORDER BY {column} DESC LIMIT 1")[0] == expected_close


@pytest.mark.asyncio
@pytest.mark.parametrize(("period", "dependency"), [("5m", "1分K"), ("1d", "日K"), ("1w", "日K")])
async def test_generation_with_no_base_reports_dependency_without_provider(app, monkeypatch, period, dependency):
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("empty generation must stay local"))
    monkeypatch.setattr(app.state.capabilities, "has", lambda cap: False)
    started = await kline.sync_period_job(SimpleNamespace(app=app), {"period": period})
    job = await wait_for_job(started["job_id"])
    assert job["status"] == "failed", job
    assert dependency in job["error"]


@pytest.mark.asyncio
async def test_daily_generation_uses_local_pipeline_and_refreshes_cached_data(app, monkeypatch):
    from app.services.public_query_cache import public_query_cache

    repo = app.state.repo
    daily = pl.DataFrame({
        "symbol": ["600000.SH"] * 2, "date": [date(2026, 10, 21), date(2026, 10, 26)],
        "open": [20., 10.], "high": [22., 11.], "low": [18., 9.], "close": [20., 10.],
        "volume": [100., 200.], "amount": [2000., 2000.],
    })
    repo.append_daily(daily)
    factors = repo.store.data_dir / "adj_factor/all.parquet"
    factors.parent.mkdir(exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH"] * 2, "trade_date": daily["date"], "ex_factor": [1., 2.],
    }).write_parquet(factors)
    originals = {path: path.read_bytes() for path in (repo.store.data_dir / "kline_daily").rglob("*.parquet")}
    monkeypatch.setattr(app.state.capabilities, "has", lambda cap: False)
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("daily generation must stay local"))
    monkeypatch.setattr(kline.kline_sync, "sync_adj_factor", lambda *a, **kw: pytest.fail("factors must stay local"))
    monkeypatch.setattr(kline.kline_sync, "sync_and_persist_daily_batch", lambda *a, **kw: pytest.fail("daily must stay local"))

    async def generation_body():
        return {"period": "1d"}

    async def generate():
        started = await kline.sync_period_job(SimpleNamespace(app=app, json=generation_body), {"period": "1d"})
        job = await wait_for_job(started["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["result"]["rows"] == 2
        assert job["result"]["source"] == "1d"
        assert all(path.read_bytes() == contents for path, contents in originals.items())
        assert repo.execute_one("SELECT count(*) FROM kline_enriched")[0] == 2
        return repo.execute_all("SELECT raw_close, close FROM kline_enriched ORDER BY date")

    assert await generate() == [(20., 10.), (10., 10.)]
    cached, _ = repo.get_enriched_latest()
    assert cached["close"].to_list() == [10.]
    public_query_cache.get("data:enriched", "local-generation", 60, lambda: "stale")
    pl.DataFrame({
        "symbol": ["600000.SH"] * 2, "trade_date": daily["date"], "ex_factor": [1., 4.],
    }).write_parquet(factors)
    assert await generate() == [(20., 5.), (10., 10.)]
    assert public_query_cache.get("data:enriched", "local-generation", 60, lambda: "fresh") == "fresh"
    assert repo.get_enriched_range(date(2026, 10, 21), date(2026, 10, 21))["close"].to_list() == [5.]


@pytest.mark.asyncio
async def test_explicit_daily_history_range_retains_legacy_entrypoint(app, monkeypatch):
    called = []

    async def extend(request):
        called.append(request)
        return {"job_id": "legacy-daily", "status": "started"}

    monkeypatch.setattr(kline, "extend_history", extend)
    request = SimpleNamespace(app=app)
    result = await kline.sync_period_job(request, {"period": "1d", "value": 1, "unit": "month"})
    assert result["job_id"] == "legacy-daily"
    assert called == [request]


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "unit"), [(6, "month"), (1, "year")])
async def test_daily_profile_acquisition_creates_history_job_with_selected_range(app, monkeypatch, value, unit):
    from app.services import extend_history
    calls = []
    def run(repo, caps, quantity, range_unit, on_progress):
        calls.append((repo, caps, quantity, range_unit))
        on_progress("extend_history", 100, "历史日K获取完成", stage_pct=100)
        return {"daily_rows": 4, "universe_size": 4, "earliest_after": "2025-10-27"}
    monkeypatch.setattr(extend_history, "run_extend_history", run)
    monkeypatch.setattr(kline_periods, "generate_period", lambda *a, **kw: pytest.fail("daily profile must acquire history"))
    body = {"period": "1d", "value": value, "unit": unit}
    async def json_body():
        return body
    started = await kline.sync_period_job(SimpleNamespace(app=app, json=json_body), body)
    job = await wait_for_job(started["job_id"])
    assert calls == [(app.state.repo, app.state.capabilities, value, unit)]
    assert job["kind"] == "extend_history"
    assert job["request"] == {"value": value, "unit": unit}
    assert job["status"] == "succeeded"
    assert job["result"]["daily_rows"] == 4
    assert job["progress"] == job["stage_pct"] == 100


def test_daily_profile_without_daily_capability_rejects_before_creating_history_task(app, monkeypatch):
    monkeypatch.setattr(app.state.capabilities, "has", lambda cap: False)
    with TestClient(app) as client:
        response = client.post("/api/kline/sync_period", json={"period": "1d", "value": 6, "unit": "month"})
    assert response.status_code == 403
    assert pipeline_jobs.job_store.list_recent() == []


@pytest.mark.asyncio
async def test_daily_generation_cancels_before_publishing_and_releases_slot(app, monkeypatch):
    repo = app.state.repo
    daily = pl.DataFrame({
        "symbol": ["600000.SH"], "date": [date(2026, 10, 26)],
        "open": [10.], "high": [11.], "low": [9.], "close": [10.5],
        "volume": [100.], "amount": [1000.],
    })
    repo.append_daily(daily)
    repo.append_enriched(daily.with_columns(pl.lit(99.).alias("close")))
    path = repo.store.data_dir / "kline_daily_enriched/date=2026-10-26/part.parquet"
    original = path.read_bytes()
    progress = pipeline_jobs.job_store.progress

    def cancel_after_batch(job_id, stage, pct, msg, **kwargs):
        if stage == "compute_enriched" and pct > 0:
            pipeline_jobs.job_store.terminate(job_id, "cancel local generation")
        progress(job_id, stage, pct, msg, **kwargs)

    monkeypatch.setattr(pipeline_jobs.job_store, "progress", cancel_after_batch)
    started = await kline.sync_period_job(SimpleNamespace(app=app), {"period": "1d"})
    job = await wait_for_job(started["job_id"])
    assert job["status"] == "cancelled", job
    assert path.read_bytes() == original
    assert not any((repo.store.data_dir / ".staging/enriched_rebuild").glob("*"))
    assert pipeline_jobs.try_acquire_run_slot("after-cancel")
    pipeline_jobs.release_run_slot("after-cancel")
