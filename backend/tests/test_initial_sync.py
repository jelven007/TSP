import json
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import initial_sync


def test_empty_deployment_needs_initial_sync(tmp_path):
    (tmp_path / "kline_daily").mkdir()
    assert initial_sync.needs_initial_sync(tmp_path)


def test_existing_daily_data_is_not_treated_as_new_install(tmp_path):
    part = tmp_path / "kline_daily" / "date=2026-10-15"
    part.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600001.SH"]}).write_parquet(part / "part.parquet")
    assert not initial_sync.needs_initial_sync(tmp_path)


def test_partial_initial_sync_remains_pending_after_restart(tmp_path):
    initial_sync.mark_pending(tmp_path)
    part = tmp_path / "kline_daily"
    part.mkdir()
    pl.DataFrame({"symbol": ["600001.SH"]}).write_parquet(part / "part.parquet")
    assert initial_sync.needs_initial_sync(tmp_path)


def test_live_daily_write_cannot_hide_first_install(tmp_path):
    initial_sync.prepare_initial_sync(tmp_path)
    part = tmp_path / "kline_daily"
    part.mkdir()
    pl.DataFrame({"symbol": ["600001.SH"]}).write_parquet(part / "part.parquet")
    assert initial_sync.needs_initial_sync(tmp_path)


def test_startup_schedules_once_without_blocking_requests(tmp_path, monkeypatch):
    calls = []

    async def trigger(*args, **kwargs):
        calls.append(kwargs)
        return {"job_id": "initial-job", "reused": False}

    monkeypatch.setattr("app.api.pipeline.trigger_pipeline_job", trigger)
    state = SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        capabilities=object(), quote_service=None,
    )

    async def exercise():
        assert await initial_sync.start_initial_sync(state) == "initial-job"
        assert await initial_sync.start_initial_sync(state) is None

    import asyncio
    asyncio.run(exercise())
    assert calls == [{"source": "startup"}]


def test_failed_job_does_not_mark_initial_sync_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(initial_sync, "check_provider_routes", lambda: None)
    def fail(*args, **kwargs):
        raise RuntimeError("source unavailable")
    monkeypatch.setattr("app.jobs.daily_pipeline.run_now", fail)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(RuntimeError, match="source unavailable"):
        initial_sync.run_initial_sync(repo, object())
    assert initial_sync.needs_initial_sync(tmp_path)
    assert json.loads((tmp_path / "initial_sync.json").read_text())["status"] == "pending"


@pytest.fixture
def four_board_pipeline(tmp_path, monkeypatch):
    from datetime import date, datetime

    from app.config import settings
    from app.data_providers import custom
    from app.services import preferences
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
    from app.tickflow.repository import DataStore, KlineRepository

    symbols = ["600001.SH", "000001.SZ", "300001.SZ", "688001.SH"]
    calls = []
    day = date(2026, 10, 15)

    def daily(selected, **kwargs):
        calls.append(("daily", selected, kwargs))
        return pl.DataFrame({
            "symbol": selected, "date": [day] * len(selected),
            **{key: [10.0] * len(selected) for key in ("open", "high", "low", "close")},
            "volume": [100.0] * len(selected), "amount": [100_000.0] * len(selected),
        })

    def minute(selected, **kwargs):
        calls.append(("minute", selected, kwargs))
        return daily(selected).rename({"date": "datetime"}).with_columns(
            pl.lit(datetime(2026, 10, 15, 9, 31)).alias("datetime"),
        )

    def financial(table, selected, **kwargs):
        calls.append((table, selected, kwargs))
        return pl.DataFrame({
            "symbol": selected, "period_end": [date(2026, 6, 30)] * len(selected),
            "announce_date": [date(2026, 8, 30)] * len(selected),
            "total_shares": [1_000_000.0] * len(selected),
            "float_shares": [500_000.0] * len(selected),
        })

    provider = SimpleNamespace(
        get_instruments=lambda asset_type: [{"symbol": s, "name": s} for s in symbols],
        get_daily=daily, get_minute=minute, get_financials=financial,
        get_adj_factors=lambda *args, **kwargs: pl.DataFrame(),
        get_realtime=lambda: [{"symbol": s, "last": 99.0} for s in symbols],
        get_depth_batch=lambda selected: {s: {"bid_prices": [9.0]} for s in selected},
    )
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)
    monkeypatch.setattr(custom, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(initial_sync, "get_provider", lambda name: provider)
    monkeypatch.setattr(initial_sync, "check_provider_routes", lambda: None)
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    preferences.save({
        "pipeline_pull_a_share": False, "pipeline_pull_index": False,
        "pipeline_pull_etf": False, "minute_sync_enabled": False,
        "pipeline_regime_enabled": False,
    })
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "service_role", "platform")
    monkeypatch.setattr("app.jobs.daily_pipeline.cn_today", lambda: day)
    repo = KlineRepository(DataStore(tmp_path))
    caps = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=100, rpm=None)})
    yield repo, caps, provider, symbols, calls
    repo.db.close()
    preferences._invalidate_cache()


def test_first_install_persists_all_four_boards_and_completes_only_after_release(
    four_board_pipeline,
):
    repo, caps, _provider, symbols, calls = four_board_pipeline
    result = initial_sync.run_initial_sync(repo, caps)
    assert result["initial_sync"] is True
    assert set(result["universe_boards"]) == {"沪主板", "深主板", "创业板", "科创板"}
    assert result["daily_rows"] == result["minute_rows"] == 4
    assert all(rows == 4 for rows in result["financial_rows"].values())
    for stage, selected, kwargs in calls:
        assert set(selected) == set(symbols), stage
        if stage in result["financial_rows"]:
            assert kwargs["latest_only"] is False
    daily_call = next(kwargs for stage, _, kwargs in calls if stage == "daily")
    assert (daily_call["end_time"] - daily_call["start_time"]).days == 365
    official = pl.read_parquet(repo.store.data_dir / "kline_daily/date=2026-10-15/part.parquet")
    assert official["close"].to_list() == [10.0] * 4
    quote = pl.read_parquet(repo.store.data_dir / "market_snapshot/quotes.parquet")
    assert quote["last"].to_list() == [99.0] * 4
    assert quote["snapshot_source"].to_list() == ["rustdx"] * 4
    marker = json.loads((repo.store.data_dir / "initial_sync.json").read_text())
    assert marker["data_release_id"] == result["data_release_id"]
    assert not initial_sync.needs_initial_sync(repo.store.data_dir)


def test_partial_quotes_leave_initialization_pending_and_preserve_snapshot(
    four_board_pipeline,
):
    repo, caps, provider, symbols, _ = four_board_pipeline
    initial_sync.run_initial_sync(repo, caps)
    target = repo.store.data_dir / "market_snapshot/quotes.parquet"
    before = target.read_bytes()
    provider.get_realtime = lambda: [{"symbol": symbols[0], "last": 88.0}]
    with pytest.raises(RuntimeError, match="实时行情同步缺少 3"):
        initial_sync.run_initial_sync(repo, caps)
    assert initial_sync.needs_initial_sync(repo.store.data_dir)
    assert target.read_bytes() == before


def test_partial_daily_does_not_complete_initialization(four_board_pipeline):
    repo, caps, provider, _symbols, _ = four_board_pipeline
    original = provider.get_daily
    provider.get_daily = lambda selected, **kwargs: original(selected[:1], **kwargs)
    with pytest.raises(RuntimeError, match="首次日K同步不完整"):
        initial_sync.run_initial_sync(repo, caps)
    assert initial_sync.needs_initial_sync(repo.store.data_dir)


def test_minute_provider_error_does_not_complete_initialization(four_board_pipeline):
    repo, caps, provider, _, _ = four_board_pipeline
    def fail(*args, **kwargs):
        raise RuntimeError("minute transport failure")
    provider.get_minute = fail
    with pytest.raises(RuntimeError, match="minute transport failure"):
        initial_sync.run_initial_sync(repo, caps)
    assert initial_sync.needs_initial_sync(repo.store.data_dir)


def test_empty_financial_table_does_not_complete_initialization(four_board_pipeline):
    repo, caps, provider, _, _ = four_board_pipeline
    original = provider.get_financials
    provider.get_financials = lambda table, selected, **kwargs: (
        pl.DataFrame() if table == "income" else original(table, selected, **kwargs)
    )
    with pytest.raises(RuntimeError, match="首次财务同步返回 0 行: income"):
        initial_sync.run_initial_sync(repo, caps)
    assert initial_sync.needs_initial_sync(repo.store.data_dir)


def test_financial_retry_requires_fresh_data_even_when_old_tables_exist(four_board_pipeline):
    repo, caps, provider, _, _ = four_board_pipeline
    initial_sync.run_initial_sync(repo, caps)
    previous = (repo.store.data_dir / "financials/income/part.parquet").read_bytes()
    provider.get_financials = lambda *args, **kwargs: pl.DataFrame()
    with pytest.raises(RuntimeError, match="首次财务同步返回 0 行"):
        initial_sync.run_initial_sync(repo, caps)
    assert initial_sync.needs_initial_sync(repo.store.data_dir)
    assert (repo.store.data_dir / "financials/income/part.parquet").read_bytes() == previous


def test_unavailable_routes_fail_before_fetch(monkeypatch):
    monkeypatch.setattr(
        "app.services.provider_audit.audit_provider_routes",
        lambda: {"unavailable": ["daily", "financial"]},
    )
    with pytest.raises(RuntimeError, match="daily, financial"):
        initial_sync.check_provider_routes()


def test_marker_write_error_does_not_leave_queued_job(tmp_path, monkeypatch):
    import asyncio

    from app.api import pipeline
    from app.services.pipeline_jobs import JobStore

    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline, "job_store", store)
    def fail(*args):
        raise OSError("read-only disk")
    monkeypatch.setattr(initial_sync, "mark_pending", fail)
    response = asyncio.run(pipeline.trigger_pipeline_job(
        SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), object(),
    ))
    job = store.get(response["job_id"])
    assert job["status"] == "failed"
    assert "read-only disk" in job["error"]
    assert store.active_id() is None


def test_shutdown_waits_for_startup_worker_to_release_resources(tmp_path, monkeypatch):
    import asyncio
    import threading

    from app.api import pipeline
    from app.services import heavy_job_limiter, pipeline_jobs
    from app.services.heavy_job_limiter import HeavyJobLimiter
    from app.services.pipeline_jobs import JobStore

    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", HeavyJobLimiter(2))
    entered, finish = threading.Event(), threading.Event()
    events = []
    def run(repo, caps, on_progress):
        entered.set()
        assert finish.wait(3)
        on_progress("sync_daily", 20, "cancel checkpoint")
        pytest.fail("shutdown must request cooperative cancellation")
    monkeypatch.setattr(initial_sync, "run_initial_sync", run)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        refresh_cache=lambda: events.append("refreshed"),
    )

    async def exercise():
        response = await pipeline.trigger_pipeline_job(repo, object(), source="startup")
        assert await asyncio.to_thread(entered.wait, 1)
        shutdown = asyncio.create_task(pipeline.stop_initial_sync_job(response["job_id"]))
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert store.get(response["job_id"])["status"] == "cancelling"
        finish.set()
        await shutdown
        assert store.get(response["job_id"])["status"] == "cancelled"
        assert events == ["refreshed"]

    asyncio.run(exercise())
