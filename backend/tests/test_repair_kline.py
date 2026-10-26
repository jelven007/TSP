import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta
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


@pytest.mark.parametrize("period", ["1m"])
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
        assert result["rows"] == 2
        assert result["period"] == period
    assert calls == [(sorted(SYMBOLS), datetime.combine(DAY, datetime.min.time()),
                      datetime.combine(DAY, datetime.max.time()), "stock", period)] * 2
    assert app.state.repo.execute_one(f"SELECT count(*) FROM {kline_periods.TABLES[period]}")[0] == result["rows"]
    assert (app.state.repo.store.data_dir / "instruments/instruments.parquet").read_bytes() == catalog


@pytest.mark.parametrize(("period", "count"), [("5m", 2), ("10m", 1), ("15m", 1)])
def test_derived_minute_repair_aggregates_local_minutes_without_provider(app, monkeypatch, period, count):
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: pytest.fail("derived minutes must stay local"))
    kline_periods.persist_period(app.state.repo, "1m", minute_frame())
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period=period)
    assert result["source"] == "1m"
    assert result["rows"] == count
    assert app.state.repo.execute_one(
        f"SELECT sum(volume), sum(amount) FROM {kline_periods.TABLES[period]}",
    ) == (300., 3000.)


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_derived_minute_repair_without_base_data_explains_the_missing_dependency(app, monkeypatch, period):
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("empty base must stay local"))
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period=period)
    assert "1分K" in result["error"]
    assert not any((app.state.repo.store.data_dir / kline_periods.TABLES[period]).rglob("*.parquet"))


def test_weekly_repair_includes_the_week_containing_the_selected_start_date(app, monkeypatch):
    kline_periods.persist_period(
        app.state.repo, "1d", pl.DataFrame({
            "symbol": [SYMBOLS[0]] * 3, "date": [date(2026, 10, day) for day in (21, 22, 23)],
            "open": [10., 11., 12.], "high": [12., 13., 14.], "low": [9., 10., 11.],
            "close": [11., 12., 13.], "volume": [100., 100., 100.], "amount": [1000., 1000., 1000.],
        }),
    )
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("weekly repair must stay local"))
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, date(2026, 10, 21), period="1w")
    assert result["rows"] == 1
    assert app.state.repo.execute_one("SELECT date FROM kline_weekly")[0] == date(2026, 10, 19)


def test_daily_repair_keeps_the_original_pipeline_and_cache_refresh(app, monkeypatch):
    calls = []
    monkeypatch.setattr("app.jobs.daily_pipeline.run_now", lambda *args, **kwargs: calls.append(kwargs) or {"daily_rows": 10})
    monkeypatch.setattr(app.state.repo, "refresh_cache", lambda: calls.append("refresh"))
    assert repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY) == {"daily_rows": 10}
    assert calls == [{"on_progress": None, "override_start_date": DAY}, "refresh"]


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
async def test_repair_api_keeps_selected_period_and_reports_real_completion(app, monkeypatch, period):
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("local repair has no route gate"))
    kline_periods.persist_period(app.state.repo, "1m", minute_frame())
    app.state.capabilities = SimpleNamespace(has=lambda _: False)
    result = await kline.repair_daily(SimpleNamespace(
        app=app, json=lambda: async_body({"start_date": DAY.isoformat(), "period": period}),
    ))
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
    job = pipeline_jobs.job_store.get(result["job_id"])
    assert job["status"] == "succeeded", job
    assert job["request"] == {"start_date": DAY.isoformat(), "period": period}
    assert job["result"]["rows"] == (2 if period == "5m" else 1)
    assert job["result"]["period"] == period
    assert job["stage_pct"] == job["progress"] == 100


async def async_body(body):
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "1d"])
async def test_repair_api_uses_custom_route_without_tickflow_entitlement(app, monkeypatch, period):
    app.state.capabilities = SimpleNamespace(has=lambda _: False)
    calls = []
    frame = minute_frame()
    if period == "1d":
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
        assert job["result"]["rows"] == 2


@pytest.mark.asyncio
async def test_repair_cancel_after_fetch_stops_before_writing_and_releases_slot(app, monkeypatch):
    def fetch(*args, **kwargs):
        pipeline_jobs.job_store.terminate(pipeline_jobs.job_store.active_id(), "用户取消")
        return minute_frame()

    provider = SimpleNamespace(capabilities=ProviderCapabilities(minute=True), get_klines=fetch)
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: provider)
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "custom")
    result = await kline.repair_daily(SimpleNamespace(
        app=app, json=lambda: async_body({"start_date": DAY.isoformat(), "period": "1m"}),
    ))
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running", "cancelling"}:
            await asyncio.sleep(0.01)
    assert pipeline_jobs.job_store.get(result["job_id"])["status"] == "cancelled"
    assert not any((app.state.repo.store.data_dir / "kline_minute").rglob("*.parquet"))
    assert pipeline_jobs._run_slot_owner is None


@pytest.mark.parametrize("period", ["1m", "1d"])
def test_repair_api_checks_selected_tickflow_entitlement(app, monkeypatch, period):
    app.state.capabilities = SimpleNamespace(has=lambda _: False)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tickflow")
    with TestClient(app) as client:
        response = client.post("/api/kline/repair_daily", json={"start_date": DAY.isoformat(), "period": period})
    assert response.status_code == 403
    assert pipeline_jobs.job_store.list_recent() == []


def test_weekly_repair_api_uses_local_daily_without_tickflow_entitlement(app, monkeypatch):
    app.state.capabilities = SimpleNamespace(has=lambda _: False)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    with TestClient(app) as client:
        response = client.post("/api/kline/repair_daily", json={"start_date": DAY.isoformat(), "period": "1w"})
    assert response.status_code == 200


@pytest.mark.parametrize("period", ["invalid", "../1m", None, []])
def test_repair_api_rejects_invalid_periods_before_creating_jobs(app, period):
    with TestClient(app) as client:
        response = client.post("/api/kline/repair_daily", json={"start_date": DAY.isoformat(), "period": period})
    assert response.status_code == 422
    assert pipeline_jobs.job_store.list_recent() == []


def bars(symbols, stamps, period="1m", price=10.):
    column = "date" if period in ("1d", "1w") else "datetime"
    keys = [(symbol, stamp) for symbol in symbols for stamp in stamps]
    return pl.DataFrame({
        "symbol": [key[0] for key in keys], column: [key[1] for key in keys],
        **{c: [price] * len(keys) for c in ("open", "high", "low", "close", "volume", "amount")},
    })


@pytest.fixture
def missing_source(app, monkeypatch):
    calls = []
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 10, 26, 9, 33))
    monkeypatch.setattr("app.jobs.daily_pipeline.run_now", lambda *a, **kw: pytest.fail("missing must not run full acquisition"))
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", lambda **kw: 1)

    def fetch(symbols, start, end, *, asset_type, freq):
        calls.append((symbols, start, end, freq))
        if freq in ("1d", "1w"):
            return bars(symbols, [start.date()], freq)
        step = int(freq[:-1])
        stamps = [start + timedelta(minutes=i) for i in range(0, int((end - start).total_seconds() // 60) + 1, step)]
        return bars(symbols, stamps, freq)

    provider = SimpleNamespace(capabilities=ProviderCapabilities(daily=True, minute=True), get_klines=fetch)
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: provider)
    return calls, provider


@pytest.mark.parametrize("period", ["1m", "5m", "10m", "15m", "1d", "1w"])
def test_missing_complete_period_makes_no_provider_requests(app, missing_source, period):
    day = date(2026, 10, 23)
    if period in ("1d", "1w"):
        stamps = [day if period == "1d" else date(2026, 10, 19)]
    else:
        step = int(period[:-1])
        stamps = [datetime.combine(day, datetime.min.time()) + timedelta(minutes=base + i)
                  for base in (570, 780) for i in range(step, 121, step)]
    kline_periods.persist_period(app.state.repo, period, bars(SYMBOLS, stamps, period))
    result = repair_daily.run_repair_daily(
        app.state.repo, app.state.capabilities, day, end_date=day, period=period, mode="missing",
    )
    assert result["rows"] == result["missing_before"] == result["remaining"] == result["requests"] == 0
    assert "error" not in result
    assert missing_source[0] == []


def test_missing_daily_detects_single_stock_inside_existing_partition(app, missing_source, monkeypatch):
    day = date(2026, 10, 23)
    kline_periods.persist_period(app.state.repo, "1d", bars(SYMBOLS[1:], [day], "1d", price=25.))
    recomputed = []
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", lambda **kw: recomputed.append(kw) or 1)
    result = repair_daily.run_repair_daily(
        app.state.repo, app.state.capabilities, day, end_date=day, mode="missing",
    )
    assert result["rows"] == result["missing_before"] == 1
    assert result["remaining"] == 0
    assert missing_source[0] == [([SYMBOLS[0]], datetime(2026, 10, 23), datetime(2026, 10, 23, 23, 59, 59, 999999), "1d")]
    assert recomputed == [{"data_dir": app.state.repo.store.data_dir, "symbols": [SYMBOLS[0]], "on_batch_done": None}]
    assert app.state.repo.execute_one("SELECT close FROM kline_daily WHERE symbol = '000001.SZ'")[0] == 25.


def test_missing_minute_requests_only_internal_contiguous_gaps_and_preserves_source_extras(app, missing_source):
    stamps = [datetime(2026, 10, 26, 9, i) for i in (31, 32, 33)]
    frame = bars(SYMBOLS, stamps).filter(
        ~((pl.col("symbol") == SYMBOLS[0]) & pl.col("datetime").is_in(stamps[1:])),
    )
    kline_periods.persist_period(app.state.repo, "1m", frame)
    calls, provider = missing_source

    def fetch(symbols, start, end, **kw):
        calls.append((symbols, start, end, kw["freq"]))
        return bars(SYMBOLS, stamps, price=99.)
    provider.get_klines = fetch
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period="1m", mode="missing")
    assert calls == [([SYMBOLS[0]], stamps[1], stamps[2], "1m")]
    assert result["rows"] == 2
    assert result["remaining"] == 0
    assert app.state.repo.execute_one(
        "SELECT close FROM kline_minute WHERE symbol = '600000.SH' ORDER BY datetime LIMIT 1",
    )[0] == 10.
    assert app.state.repo.execute_one("SELECT close FROM kline_minute WHERE symbol = '000001.SZ' LIMIT 1")[0] == 10.


def test_missing_excludes_holidays_prelisting_days_and_unfinished_daily_bar(app, missing_source, monkeypatch):
    monkeypatch.setattr(app.state.repo, "get_instruments", lambda: pl.DataFrame({
        "symbol": SYMBOLS, "listing_date": [DAY] * 4,
    }))
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, date(2026, 10, 1), mode="missing")
    assert result["missing_before"] == result["rows"] == 0
    assert missing_source[0] == []


def test_missing_unknown_calendar_fails_before_requesting_stock_data(app, missing_source, monkeypatch):
    monkeypatch.setattr("app.services.data_integrity._trading_calendar", lambda: None)
    result = repair_daily.run_repair_daily(
        app.state.repo, app.state.capabilities, date(2025, 1, 2), end_date=date(2025, 1, 3), mode="missing",
    )
    assert "交易日历" in result["error"]
    assert missing_source[0] == []


def test_missing_partial_response_reports_real_remaining_gaps(app, missing_source):
    missing_source[1].get_klines = lambda *a, **kw: bars([SYMBOLS[0]], [date(2026, 10, 23)], "1d")
    result = repair_daily.run_repair_daily(
        app.state.repo, app.state.capabilities, date(2026, 10, 23), end_date=date(2026, 10, 23), mode="missing",
    )
    assert result["missing_before"] == 4
    assert result["rows"] == 1
    assert result["remaining"] == 3
    assert "3" in result["error"]


@pytest.mark.parametrize("period", ["1m", "1d", "1w"])
def test_only_missing_writer_preserves_existing_and_counts_actual_new_rows(app, period):
    stamps = [DAY] if period in ("1d", "1w") else [datetime(2026, 10, 26, 9, 31)]
    kline_periods.persist_period(app.state.repo, period, bars([SYMBOLS[0]], stamps, period))
    written = kline_periods.persist_period(app.state.repo, period, bars(SYMBOLS, stamps, period, price=99.), only_missing=True)
    assert written == 3
    assert kline_periods.persist_period(app.state.repo, period, bars(SYMBOLS, stamps, period), only_missing=True) == 0
    assert app.state.repo.execute_one(f"SELECT close FROM {kline_periods.TABLES[period]} WHERE symbol = '600000.SH'")[0] == 10.


@pytest.mark.asyncio
async def test_missing_api_keeps_mode_and_cancels_before_write(app, missing_source, monkeypatch):
    def fetch(*a, **kw):
        pipeline_jobs.job_store.terminate(pipeline_jobs.job_store.active_id(), "用户取消")
        return bars(SYMBOLS, [datetime(2026, 10, 26, 9, 31)])
    missing_source[1].get_klines = fetch
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "custom")
    result = await kline.repair_daily(SimpleNamespace(
        app=app, json=lambda: async_body({"start_date": DAY.isoformat(), "period": "1m", "mode": "missing"}),
    ))
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running", "cancelling"}:
            await asyncio.sleep(0.01)
    job = pipeline_jobs.job_store.get(result["job_id"])
    assert job["request"]["mode"] == "missing"
    assert job["status"] == "cancelled"
    assert not any((app.state.repo.store.data_dir / "kline_minute").rglob("*.parquet"))
    assert pipeline_jobs._run_slot_owner is None


@pytest.mark.parametrize("mode", ["invalid", None, [], 1])
def test_repair_api_rejects_invalid_modes_before_creating_jobs(app, mode):
    with TestClient(app) as client:
        response = client.post("/api/kline/repair_daily", json={"start_date": DAY.isoformat(), "mode": mode})
    assert response.status_code == 422
    assert pipeline_jobs.job_store.list_recent() == []


def test_missing_daily_whole_day_only_requests_that_day(app, missing_source):
    kline_periods.persist_period(app.state.repo, "1d", bars(SYMBOLS, [date(2026, 10, 22)], "1d"))
    result = repair_daily.run_repair_daily(
        app.state.repo, app.state.capabilities, date(2026, 10, 22), end_date=date(2026, 10, 23), mode="missing",
    )
    assert result["rows"] == 4
    assert len(missing_source[0]) == 1
    assert missing_source[0][0][1].date() == date(2026, 10, 23)


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_missing_intraday_excludes_lunch_future_bars_and_separates_nonadjacent_gaps(app, missing_source, monkeypatch, period):
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 10, 26, 13, 16))
    step = int(period[:-1])
    stamps = [datetime(2026, 10, 26) + timedelta(minutes=base + i)
              for base, duration in ((570, 120), (780, 16)) for i in range(step, duration + 1, step)]
    gaps = [stamps[0], stamps[-1]]
    frame = bars(SYMBOLS, stamps, period).filter(
        ~((pl.col("symbol") == SYMBOLS[0]) & pl.col("datetime").is_in(gaps)),
    )
    kline_periods.persist_period(app.state.repo, period, frame)
    base_stamps = [datetime(2026, 10, 26) + timedelta(minutes=base + i)
                   for base, duration in ((570, 120), (780, 16)) for i in range(1, duration + 1)]
    kline_periods.persist_period(app.state.repo, "1m", bars(SYMBOLS, base_stamps))
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period=period, mode="missing")
    assert missing_source[0] == []
    assert result["rows"] == 2
    assert result["remaining"] == 0


def test_missing_week_aggregates_local_daily_and_prevents_prelisting_week(app, missing_source, monkeypatch):
    monkeypatch.setattr(app.state.repo, "get_instruments", lambda: pl.DataFrame({
        "symbol": SYMBOLS, "listing_date": [date(2026, 10, 21)] + [DAY] * 3,
    }))
    kline_periods.persist_period(
        app.state.repo, "1d", bars([SYMBOLS[0]], [date(2026, 10, day) for day in (21, 22, 23)], "1d"),
    )
    missing_source[1].get_klines = lambda *a, **kw: pytest.fail("weekly missing repair must stay local")
    result = repair_daily.run_repair_daily(
        app.state.repo, app.state.capabilities, date(2026, 10, 21), end_date=date(2026, 10, 23), period="1w", mode="missing",
    )
    assert result["rows"] == 1
    assert result["remaining"] == 0
    assert missing_source[0] == []
    assert app.state.repo.execute_one("SELECT symbol, date FROM kline_weekly") == (SYMBOLS[0], date(2026, 10, 19))


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_missing_derived_minute_only_fills_complete_local_buckets(app, missing_source, monkeypatch, period):
    step = int(period[:-1])
    stamps = [datetime(2026, 10, 26, 9, 30) + timedelta(minutes=i) for i in range(1, step * 2 + 1)]
    monkeypatch.setattr("app.market_time.cn_now", lambda: stamps[-1])
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("missing must stay local"))
    frame = bars(SYMBOLS, stamps).filter(
        ~((pl.col("symbol") == SYMBOLS[0]) & (pl.col("datetime") == stamps[-1])),
    )
    kline_periods.persist_period(app.state.repo, "1m", frame)
    kline_periods.persist_period(app.state.repo, period, bars(SYMBOLS[1:], [stamps[step - 1], stamps[-1]], period, price=25.))
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period=period, mode="missing")
    assert result["missing_before"] == 2
    assert result["rows"] == result["remaining"] == 1
    assert "1分K" in result["error"]
    assert missing_source[0] == []
    assert result["source"] == "1m"
    assert app.state.repo.execute_one(f"SELECT close FROM {kline_periods.TABLES[period]} WHERE symbol = '000001.SZ' LIMIT 1")[0] == 25.


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_derived_missing_unknown_calendar_never_fetches_upstream(app, monkeypatch, period):
    monkeypatch.setattr("app.market_time.published_trading_day", lambda _: None)
    monkeypatch.setattr("app.services.data_integrity.cached_trading_calendar", lambda: set())
    monkeypatch.setattr("app.services.data_integrity._trading_calendar", lambda: pytest.fail("derived missing must not fetch a calendar"))
    result = repair_daily.run_repair_daily(
        app.state.repo, app.state.capabilities, date(2025, 1, 2), end_date=date(2025, 1, 3),
        period=period, mode="missing",
    )
    assert "交易日历" in result["error"]


def test_missing_concurrent_fill_is_preserved_and_not_counted_as_own_write(app, missing_source):
    stamps = [datetime(2026, 10, 26, 9, i) for i in (31, 32, 33)]
    kline_periods.persist_period(app.state.repo, "1m", bars(SYMBOLS[1:], stamps))
    def fetch(*a, **kw):
        kline_periods.persist_period(app.state.repo, "1m", bars([SYMBOLS[0]], stamps, price=25.))
        return bars([SYMBOLS[0]], stamps, price=99.)
    missing_source[1].get_klines = fetch
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period="1m", mode="missing")
    assert result["missing_before"] == 3
    assert result["rows"] == result["remaining"] == 0
    assert app.state.repo.execute_one("SELECT close FROM kline_minute WHERE symbol = '600000.SH' LIMIT 1")[0] == 25.


@pytest.mark.asyncio
async def test_missing_partial_failure_task_is_failed_with_written_and_remaining_counts(app, missing_source, monkeypatch):
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "custom")
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 10, 26, 16))
    missing_source[1].get_klines = lambda *a, **kw: bars([SYMBOLS[0]], [DAY], "1d")
    result = await kline.repair_daily(SimpleNamespace(
        app=app, json=lambda: async_body({"start_date": DAY.isoformat(), "mode": "missing"}),
    ))
    async with asyncio.timeout(3):
        while pipeline_jobs.job_store.get(result["job_id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(0.01)
    job = pipeline_jobs.job_store.get(result["job_id"])
    assert job["status"] == "failed"
    assert "已补1行" in job["error"]
    assert "3行" in job["error"]
    assert app.state.repo.execute_one("SELECT count(*) FROM kline_daily")[0] == 1


def test_missing_fetch_failure_reports_unfilled_records(app, missing_source):
    def fetch(*a, **kw):
        raise ConnectionError("unavailable")
    missing_source[1].get_klines = fetch
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period="1m", mode="missing")
    assert result["rows"] == 0
    assert result["remaining"] == 12
    assert "请求失败" in result["error"]


def test_missing_requests_are_batched_and_bounded_to_100_stocks(app, missing_source, monkeypatch):
    symbols = [f"{600000 + i}.SH" for i in range(133)]
    monkeypatch.setattr("app.services.instrument_sync.load_current_stock_pool", lambda *a, **kw: SimpleNamespace(symbols=symbols))
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, period="1m", mode="missing")
    assert result["rows"] == 399
    assert result["remaining"] == 0
    assert [len(call[0]) for call in missing_source[0]] == [100, 33]


def test_missing_pauses_quotes_only_for_writes_and_broadcasts_after_refresh(app, missing_source, monkeypatch):
    from app.services import kline_sync
    active = False
    writes = []
    broadcasts = []
    @contextmanager
    def paused():
        nonlocal active
        active = True
        try:
            yield
        finally:
            active = False
    fetch = missing_source[1].get_klines
    def unpaused_fetch(*a, **kw):
        assert not active
        return fetch(*a, **kw)
    missing_source[1].get_klines = unpaused_fetch
    write = kline_sync._atomic_write_parquet
    def guarded_write(frame, out):
        assert active
        writes.append(out)
        write(frame, out)
    monkeypatch.setattr(kline_sync, "_atomic_write_parquet", guarded_write)
    quote_service = SimpleNamespace(paused=paused, _broadcast_quote_updated=lambda: broadcasts.append(True))
    result = repair_daily.run_repair_daily(
        app.state.repo, app.state.capabilities, DAY, period="1m", mode="missing", quote_service=quote_service,
    )
    assert result["rows"] == 12
    assert len(writes) == 1
    assert broadcasts == [True]
    assert not active


def test_missing_daily_recomputes_real_enriched_and_refreshes_repository(app, monkeypatch):
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 10, 26, 16))
    provider = SimpleNamespace(capabilities=ProviderCapabilities(daily=True),
                               get_klines=lambda symbols, *a, **kw: bars(symbols, [DAY], "1d", price=25.))
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda _: provider)
    monkeypatch.setattr("app.jobs.daily_pipeline.run_now", lambda *a, **kw: pytest.fail("must not reacquire other datasets"))
    result = repair_daily.run_repair_daily(app.state.repo, app.state.capabilities, DAY, mode="missing")
    assert result["rows"] == result["enriched_rows"] == 4
    assert result["remaining"] == 0
    assert app.state.repo.get_enriched_latest()[1] == DAY
    assert app.state.repo.execute_one("SELECT raw_close FROM kline_enriched WHERE symbol = '600000.SH'")[0] == 25.
