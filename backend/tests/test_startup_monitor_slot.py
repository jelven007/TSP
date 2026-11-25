"""Startup gap backfills must leave the live monitor repair slot available."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.services import pipeline_jobs
from app.services import post_close_sync as correction


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 23, 14, 45, tzinfo=CN_TZ)]
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    calls = []
    monkeypatch.setattr(settings, "service_role", "platform")
    monkeypatch.setattr(correction, "cn_now", lambda: clock[0])
    monkeypatch.setattr(correction.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(correction.trading_day, "is_trading_day", lambda _: True)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: store.start(jid) or fn())
    monkeypatch.setattr(correction, "finalize_daily_settlement", lambda *a: None)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: {"release_id": "release"})
    pipeline_jobs.release_run_slot()

    def repair(*args, **kwargs):
        calls.append(kwargs["period"])
        kwargs["on_progress"]("sync", 100, "done")
        return {"rows": 10}

    monkeypatch.setattr(correction, "run_repair_daily", repair)
    yield SimpleNamespace(repo=repo, clock=clock, store=store, calls=calls)
    pipeline_jobs.release_run_slot()


@pytest.mark.parametrize("hour,minute", [(9, 15), (10, 0), (11, 15), (14, 30), (14, 45), (15, 0)])
def test_live_startup_defers_without_claiming_monitor_slot(env, hour, minute):
    env.clock[0] = env.clock[0].replace(hour=hour, minute=minute)
    state = correction.request_startup(env.repo, "caps")
    assert state["status"] == "pending"
    assert not state.get("job_id")
    assert env.calls == []
    assert pipeline_jobs.try_acquire_run_slot("monitor-input-repair")
    pipeline_jobs.release_run_slot("monitor-input-repair")
    env.clock[0] = env.clock[0].replace(hour=15, minute=1)
    resumed = correction.request_startup(env.repo, "caps", restart=False)
    assert resumed["status"] == "succeeded"
    assert env.calls == list(correction.PERIODS)


def test_premarket_worker_yields_at_live_boundary_and_keeps_partial_diagnostics(env, monkeypatch):
    env.clock[0] = env.clock[0].replace(hour=9, minute=14)

    def crosses_boundary(*args, **kwargs):
        env.calls.append(kwargs["period"])
        env.clock[0] = env.clock[0].replace(minute=15)
        kwargs["on_progress"]("sync", 20, "partial data written")
        pytest.fail("worker continued after live boundary")

    monkeypatch.setattr(correction, "run_repair_daily", crosses_boundary)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: pytest.fail("partial publish"))
    deferred = correction.request_startup(env.repo, "caps")
    assert deferred["status"] == "pending"
    assert env.store.get(deferred["job_id"])["status"] == "cancelled"
    assert env.calls == ["1m"]
    assert pipeline_jobs.try_acquire_run_slot("monitor-input-repair")


def test_explicit_cancellation_is_terminal_even_at_live_boundary(env, monkeypatch):
    env.clock[0] = env.clock[0].replace(hour=9, minute=14)

    def cancelled(*args, **kwargs):
        pipeline_jobs.request_cancel(env.store._active_id)
        env.clock[0] = env.clock[0].replace(minute=15)
        kwargs["on_progress"]("sync", 20, "cancelled")

    monkeypatch.setattr(correction, "run_repair_daily", cancelled)
    assert correction.request_startup(env.repo, "caps")["status"] == "cancelled"
