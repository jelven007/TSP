"""Missing-only daily management keeps its date across busy writers/restarts."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.services import pipeline_jobs
from app.services import post_close_sync as correction


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 3, 15, 35, tzinfo=CN_TZ)]
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    calls = []
    monkeypatch.setattr(settings, "service_role", "platform")
    monkeypatch.setattr(correction, "cn_now", lambda: clock[0])
    monkeypatch.setattr(correction.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(correction.trading_day, "is_trading_day", lambda _: True)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: store.start(jid) or fn())
    pipeline_jobs.release_run_slot()

    def repair(repo, capset, start, end, **kwargs):
        assert kwargs["mode"] == "missing"
        calls.append((kwargs["period"], start, end, capset))
        kwargs["on_progress"]("sync", 100, "done")
        return {"rows": 10}

    monkeypatch.setattr(correction, "run_repair_daily", repair)
    monkeypatch.setattr(correction, "finalize_daily_settlement", lambda *a: None)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: {"release_id": "release"})
    yield SimpleNamespace(repo=repo, clock=clock, store=store, calls=calls)
    pipeline_jobs.release_run_slot()


def test_fixed_time_repairs_only_the_target_day_in_base_first_order(env, monkeypatch):
    env.clock[0] -= timedelta(seconds=1)
    assert correction.request_sync(env.repo, "caps") is None
    assert env.calls == []
    env.clock[0] += timedelta(seconds=1)
    state = correction.request_sync(env.repo, "caps")
    assert state["status"] == "succeeded"
    assert [call[0] for call in env.calls] == ["1m", "5m", "10m", "15m", "1d"]
    assert all(start == end == env.clock[0].date() for _, start, end, _ in env.calls)
    job = env.store.get(state["job_id"])
    assert job["kind"] == "daily_pipeline" and job["source"] == "scheduled"
    assert job["request"]["mode"] == "missing"
    assert job["result"]["data_release_id"] == "release"
    # Recreate the JobStore as startup does; successful receipts still deduplicate.
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=env.store._store_dir))
    correction.request_sync(env.repo, "new caps")
    correction.retry_pending(env.repo, "new caps")
    assert len(env.calls) == 5


@pytest.mark.parametrize("verdict", [False, None])
def test_nontrading_or_unknown_day_never_creates_a_request(env, monkeypatch, verdict):
    monkeypatch.setattr(correction.trading_day, "is_trading_day", lambda _: verdict)
    assert correction.request_sync(env.repo, None) is None
    assert not (env.repo.store.data_dir / "post_close_sync").exists()


def test_auto_disabled_does_not_run_pending_work(env, monkeypatch):
    assert pipeline_jobs.try_acquire_run_slot("other")
    assert correction.request_sync(env.repo, None)["status"] == "pending"
    pipeline_jobs.release_run_slot("other")
    monkeypatch.setattr(correction.preferences, "get_data_sync_auto_enabled", lambda: False)
    correction.retry_pending(env.repo, None)
    assert env.calls == []


def test_busy_writer_keeps_date_and_retries_without_reusing_its_job(env):
    other, _ = env.store.create(kind="daily_pipeline")
    env.store.start(other)
    assert pipeline_jobs.try_acquire_run_slot(other)
    state = correction.request_sync(env.repo, "old caps")
    assert state["status"] == "pending" and state.get("job_id") is None
    pipeline_jobs.release_run_slot(other)
    # An active queued task also blocks correction even if the slot is free.
    correction.retry_pending(env.repo, None)
    assert env.calls == []
    env.store.succeed(other, {})
    state = correction.retry_pending(env.repo, "live caps")
    assert state["status"] == "succeeded"
    assert state["job_id"] != other
    assert all(call[3] == "live caps" for call in env.calls)


def test_pending_from_previous_date_is_interrupted_without_repairing_history(env):
    import json

    assert pipeline_jobs.try_acquire_run_slot("other")
    correction.request_sync(env.repo, None)
    pipeline_jobs.release_run_slot("other")
    env.clock[0] += timedelta(days=1)
    state = correction.retry_pending(env.repo, None)
    assert state["status"] == "succeeded" and state["day"] == "2026-11-04"
    previous = json.loads((env.repo.store.data_dir / "post_close_sync/2026-11-03.json").read_text())
    assert previous["status"] == "interrupted"
    assert all(start.isoformat() == "2026-11-04" for _, start, _, _ in env.calls)


def test_pending_request_survives_restart_and_running_request_does_not_restart(env, monkeypatch):
    assert pipeline_jobs.try_acquire_run_slot("other")
    correction.request_sync(env.repo, None)
    pipeline_jobs.release_run_slot("other")
    restarted = pipeline_jobs.JobStore(store_dir=env.store._store_dir)
    monkeypatch.setattr(pipeline_jobs, "job_store", restarted)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: restarted.start(jid) or fn())
    assert correction.retry_pending(env.repo, None)["status"] == "succeeded"
    env.clock[0] += timedelta(days=1)
    jid, _ = restarted.create(kind="kline_correction")
    restarted.start(jid)
    correction._save(env.repo, {"day": "2026-11-04", "status": "running", "job_id": jid})
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=env.store._store_dir))
    assert correction.retry_pending(env.repo, None)["status"] == "interrupted"
    assert len(env.calls) == 5


def test_date_stays_frozen_if_execution_crosses_midnight(env, monkeypatch):
    def repair(repo, caps, start, end, **kwargs):
        env.calls.append((kwargs["period"], start, end))
        env.clock[0] = datetime(2026, 11, 4, 0, 1, tzinfo=CN_TZ)
        return {"rows": 1}
    monkeypatch.setattr(correction, "run_repair_daily", repair)
    assert correction.request_sync(env.repo, None)["day"] == "2026-11-03"
    assert all(start == end and start.isoformat() == "2026-11-03" for _, start, end in env.calls)


def test_publication_failure_is_failed_and_releases_the_execution_slot(env, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("publication unavailable")
    monkeypatch.setattr(correction, "publish_data_release", fail)
    state = correction.request_sync(env.repo, None)
    assert state["status"] == "failed"
    assert "publication unavailable" in env.store.get(state["job_id"])["error"]
    assert pipeline_jobs.try_acquire_run_slot("next")


@pytest.mark.parametrize("result", [
    {"error": "empty"}, {"remaining": 3}, {"gap_check_warning": "unknown"},
    {"warning": "not all records returned"},
])
def test_incomplete_period_fails_once_and_does_not_publish(env, monkeypatch, result):
    monkeypatch.setattr(correction, "run_repair_daily", lambda *a, **kw: result)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: pytest.fail("partial publication"))
    state = correction.request_sync(env.repo, None)
    assert state["status"] == "failed"
    assert env.store.get(state["job_id"])["status"] == "failed"
    correction.retry_pending(env.repo, None)
    assert correction.request_sync(env.repo, None)["status"] == "failed"


def test_cancelled_job_is_terminal_and_is_not_published(env, monkeypatch):
    def cancel(*args, **kwargs):
        jid = env.store._active_id
        pipeline_jobs.request_cancel(jid)
        kwargs["on_progress"]("sync", 20, "cancel")
        return {"rows": 1}
    monkeypatch.setattr(correction, "run_repair_daily", cancel)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: pytest.fail("cancel publication"))
    state = correction.request_sync(env.repo, None)
    assert state["status"] == "cancelled"
    assert env.store.get(state["job_id"])["status"] == "cancelled"
    assert pipeline_jobs.try_acquire_run_slot("next")


def test_fresh_start_retry_never_registers_or_fetches_a_day(env):
    env.clock[0] = env.clock[0].replace(hour=14)
    assert correction.retry_pending(env.repo, None) is None
    assert env.calls == []
    assert not (env.repo.store.data_dir / "post_close_sync").exists()


def test_morning_startup_and_afternoon_close_have_independent_receipts(env):
    env.clock[0] = env.clock[0].replace(hour=10, minute=0)
    startup = correction.request_startup(env.repo, "caps")
    assert startup["status"] == "succeeded" and startup["trigger"] == "startup"
    assert len(env.calls) == 5
    assert correction.request_sync(env.repo, "caps") is None
    env.clock[0] = env.clock[0].replace(hour=15, minute=35)
    close = correction.request_sync(env.repo, "caps")
    assert close["status"] == "succeeded" and close["job_id"] != startup["job_id"]
    assert len(env.calls) == 10
    # A fresh restart checks new gaps; timer retries never repeat a terminal startup.
    assert correction.request_startup(env.repo, "caps", restart=False)["job_id"] == startup["job_id"]
    assert len(env.calls) == 10
    assert correction.request_startup(env.repo, "caps")["job_id"] != startup["job_id"]
    assert len(env.calls) == 15


def test_busy_startup_retries_before_close_time(env):
    env.clock[0] = env.clock[0].replace(hour=10, minute=0)
    assert pipeline_jobs.try_acquire_run_slot("other")
    assert correction.request_startup(env.repo, "caps")["status"] == "pending"
    pipeline_jobs.release_run_slot("other")
    assert correction.request_startup(env.repo, "new caps", restart=False)["status"] == "succeeded"
    assert all(call[3] == "new caps" for call in env.calls)


@pytest.mark.parametrize("verdict", [False, None])
def test_startup_respects_nontrading_and_unknown_days(env, monkeypatch, verdict):
    monkeypatch.setattr(correction.trading_day, "is_trading_day", lambda _: verdict)
    assert correction.request_startup(env.repo, "caps") is None
    assert env.calls == []


def test_startup_pending_does_not_roll_over_into_a_new_day(env):
    assert pipeline_jobs.try_acquire_run_slot("other")
    correction.request_startup(env.repo, None)
    pipeline_jobs.release_run_slot("other")
    env.clock[0] += timedelta(days=1)
    assert correction.request_startup(env.repo, None, restart=False)["status"] == "interrupted"
    assert env.calls == []


def test_premarket_startup_does_not_fetch_unformed_bars(env, monkeypatch):
    from app.services import kline_periods
    from app.services.repair_daily import run_repair_daily
    from app.tickflow.repository import DataStore, KlineRepository
    env.clock[0] = env.clock[0].replace(hour=9, minute=0)
    monkeypatch.setattr("app.market_time.cn_now", lambda: env.clock[0])
    monkeypatch.setattr("app.market_time.cn_today", lambda: env.clock[0].date())
    monkeypatch.setattr("app.services.instrument_sync.load_current_stock_pool",
                        lambda *a, **kw: SimpleNamespace(symbols=["600000.SH"]))
    monkeypatch.setattr(kline_periods, "require_period_provider",
                        lambda *a: pytest.fail("no formed bars means no provider"))
    monkeypatch.setattr(correction, "run_repair_daily", run_repair_daily)
    repo = KlineRepository(DataStore(env.repo.store.data_dir))
    try:
        state = correction.request_startup(repo, None)
        assert state["status"] == "succeeded"
        results = env.store.get(state["job_id"])["result"]["periods"]
        assert all(r["requests"] == r["rows"] == r["remaining"] == 0 for r in results.values())
        assert not any(repo.store.data_dir.glob("kline_*/date=*/*.parquet"))
    finally:
        repo.db.close()


def test_one_period_failure_still_checks_other_periods_and_retains_diagnostics(env, monkeypatch):
    calls = []
    def partial(*args, **kwargs):
        calls.append(kwargs["period"])
        return {"remaining": 1, "warning": "halted"} if kwargs["period"] == "1m" else {"rows": 0}
    monkeypatch.setattr(correction, "run_repair_daily", partial)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: pytest.fail("partial publication"))
    state = correction.request_sync(env.repo, None)
    assert calls == list(correction.PERIODS)
    assert state["status"] == "failed"
    assert state["periods"]["1m"]["remaining"] == 1
    assert state["periods"]["1d"]["rows"] == 0


@pytest.mark.parametrize("trigger", ["scheduled", "startup"])
def test_complete_five_period_day_never_resolves_provider_or_changes_files(env, monkeypatch, trigger):
    import polars as pl

    from app.services import kline_periods
    from app.services.repair_daily import run_repair_daily
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(env.repo.store.data_dir))
    day = env.clock[0].date()
    stamps = [datetime.combine(day, datetime.min.time()) + timedelta(minutes=base + minute)
              for base in (570, 780) for minute in range(1, 121)]
    frame = pl.DataFrame({
        "symbol": ["600000.SH"] * 240, "datetime": stamps,
        **{key: [10.] * 240 for key in ("open", "high", "low", "close", "volume", "amount")},
    })
    kline_periods.persist_period(repo, "1m", frame)
    for period in ("5m", "10m", "15m"):
        kline_periods.persist_period(repo, period, kline_periods.aggregate_minute_period(frame, period))
    kline_periods.persist_period(repo, "1d", frame.head(1).drop("datetime").with_columns(pl.lit(day).alias("date")))
    monkeypatch.setattr("app.services.instrument_sync.load_current_stock_pool", lambda *a, **kw: SimpleNamespace(symbols=["600000.SH"]))
    monkeypatch.setattr("app.market_time.cn_now", lambda: env.clock[0])
    monkeypatch.setattr(kline_periods, "require_period_provider", lambda *a: pytest.fail("no gaps means no provider requests"))
    monkeypatch.setattr(correction, "run_repair_daily", run_repair_daily)
    before = {path: path.read_bytes() for path in repo.store.data_dir.glob("kline_*/date=*/*.parquet")}
    try:
        run = correction.request_startup if trigger == "startup" else correction.request_sync
        state = run(repo, "caps")
        assert state["status"] == "succeeded"
        job = env.store.get(state["job_id"])
        assert all(result["rows"] == result["requests"] == result["remaining"] == 0
                   for result in job["result"]["periods"].values())
        assert before == {path: path.read_bytes() for path in before}
    finally:
        repo.db.close()
