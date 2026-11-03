"""Fixed daily correction keeps its date and survives busy writers/restarts."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.services import kline_correction as correction
from app.services import pipeline_jobs


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
        calls.append((kwargs["period"], start, end, capset))
        kwargs["on_progress"]("sync", 100, "done")
        return {"rows": 10}

    monkeypatch.setattr(correction, "run_repair_daily", repair)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: {"release_id": "release"})
    yield SimpleNamespace(repo=repo, clock=clock, store=store, calls=calls)
    pipeline_jobs.release_run_slot()


def test_fixed_time_repairs_only_the_target_day_in_base_first_order(env, monkeypatch):
    env.clock[0] -= timedelta(seconds=1)
    assert correction.request_correction(env.repo, "caps") is None
    assert env.calls == []
    env.clock[0] += timedelta(seconds=1)
    state = correction.request_correction(env.repo, "caps")
    assert state["status"] == "succeeded"
    assert [call[0] for call in env.calls] == ["1d", "1m", "5m", "10m", "15m"]
    assert all(start == end == env.clock[0].date() for _, start, end, _ in env.calls)
    job = env.store.get(state["job_id"])
    assert job["kind"] == "kline_correction" and job["source"] == "scheduled"
    assert job["result"]["data_release_id"] == "release"
    # Recreate the JobStore as startup does; successful receipts still deduplicate.
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=env.store._store_dir))
    correction.request_correction(env.repo, "new caps")
    correction.retry_pending(env.repo, "new caps")
    assert len(env.calls) == 5


@pytest.mark.parametrize("verdict", [False, None])
def test_nontrading_or_unknown_day_never_creates_a_request(env, monkeypatch, verdict):
    monkeypatch.setattr(correction.trading_day, "is_trading_day", lambda _: verdict)
    assert correction.request_correction(env.repo, None) is None
    assert not (env.repo.store.data_dir / "kline_correction").exists()


def test_auto_disabled_does_not_run_pending_work(env, monkeypatch):
    assert pipeline_jobs.try_acquire_run_slot("other")
    assert correction.request_correction(env.repo, None)["status"] == "pending"
    pipeline_jobs.release_run_slot("other")
    monkeypatch.setattr(correction.preferences, "get_data_sync_auto_enabled", lambda: False)
    correction.retry_pending(env.repo, None)
    assert env.calls == []


def test_busy_writer_keeps_date_and_retries_without_reusing_its_job(env):
    other, _ = env.store.create(kind="daily_pipeline")
    env.store.start(other)
    assert pipeline_jobs.try_acquire_run_slot(other)
    state = correction.request_correction(env.repo, "old caps")
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
    assert pipeline_jobs.try_acquire_run_slot("other")
    correction.request_correction(env.repo, None)
    pipeline_jobs.release_run_slot("other")
    env.clock[0] += timedelta(days=1)
    state = correction.retry_pending(env.repo, None)
    assert state["status"] == "interrupted" and state["day"] == "2026-11-03"
    assert env.calls == []


def test_pending_request_survives_restart_and_running_request_does_not_restart(env, monkeypatch):
    assert pipeline_jobs.try_acquire_run_slot("other")
    correction.request_correction(env.repo, None)
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
    assert correction.request_correction(env.repo, None)["day"] == "2026-11-03"
    assert all(start == end and start.isoformat() == "2026-11-03" for _, start, end in env.calls)


def test_publication_failure_is_failed_and_releases_the_execution_slot(env, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("publication unavailable")
    monkeypatch.setattr(correction, "publish_data_release", fail)
    state = correction.request_correction(env.repo, None)
    assert state["status"] == "failed"
    assert "publication unavailable" in env.store.get(state["job_id"])["error"]
    assert pipeline_jobs.try_acquire_run_slot("next")


@pytest.mark.parametrize("result", [
    {"error": "empty"}, {"remaining": 3}, {"gap_check_warning": "unknown"},
    {"daily_rows": 0}, {"skipped_stages": ["sync_daily"]},
])
def test_incomplete_period_fails_once_and_does_not_publish(env, monkeypatch, result):
    monkeypatch.setattr(correction, "run_repair_daily", lambda *a, **kw: result)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: pytest.fail("partial publication"))
    state = correction.request_correction(env.repo, None)
    assert state["status"] == "failed"
    assert env.store.get(state["job_id"])["status"] == "failed"
    correction.retry_pending(env.repo, None)
    assert correction.request_correction(env.repo, None)["status"] == "failed"


def test_cancelled_job_is_terminal_and_is_not_published(env, monkeypatch):
    def cancel(*args, **kwargs):
        jid = env.store._active_id
        pipeline_jobs.request_cancel(jid)
        kwargs["on_progress"]("sync", 20, "cancel")
        return {"rows": 1}
    monkeypatch.setattr(correction, "run_repair_daily", cancel)
    monkeypatch.setattr(correction, "publish_data_release", lambda *a, **kw: pytest.fail("cancel publication"))
    state = correction.request_correction(env.repo, None)
    assert state["status"] == "cancelled"
    assert env.store.get(state["job_id"])["status"] == "cancelled"
    assert pipeline_jobs.try_acquire_run_slot("next")
