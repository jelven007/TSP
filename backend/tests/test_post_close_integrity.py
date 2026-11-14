"""The 15:45 job repairs only today's close inputs, with durable bounded retries."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_selection_daily_repair import env  # noqa: F401

from app.config import settings
from app.jobs import daily_pipeline
from app.market_time import CN_TZ
from app.services import pipeline_jobs
from app.tickflow.capabilities import CapabilitySet


@pytest.fixture
def integrity(request, monkeypatch):
    from app.services import post_close_integrity as repair

    inputs = request.getfixturevalue("env")
    inputs.clock[0] = datetime(2026, 11, 10, 15, 45, tzinfo=CN_TZ)
    monkeypatch.setattr(repair, "cn_now", lambda: inputs.clock[0])
    monkeypatch.setattr(repair.trading_day, "is_trading_day", lambda _: True)
    inputs.repair = repair
    return inputs


@pytest.mark.parametrize("broken", [
    "daily_missing", "daily_price", "daily_clock", "minute_missing", "minute_price",
    "enriched_missing", "enriched_price", "enriched_clock",
])
def test_1545_repairs_original_close_failures_before_readiness(integrity, broken):
    e = integrity
    folder = {"daily": "kline_daily", "minute": "kline_minute",
              "enriched": "kline_daily_enriched"}[broken.split("_")[0]]
    path = e.paths[folder]
    if broken.endswith("missing"):
        path.unlink()
    else:
        frame = pl.read_parquet(path)
        if broken.endswith("clock"):
            frame = frame.with_columns(pl.lit(0).alias("quote_ts"),
                                       pl.lit("received").alias("quote_time_basis"))
        else:
            frame = frame.with_columns(pl.lit(9.).alias(
                "raw_close" if folder.endswith("enriched") else "close"))
        frame.write_parquet(path)
    state = e.repair.request_repair(e.repo, None)
    assert state["status"] == "succeeded"
    assert e.store.get(state["job_id"])["kind"] == "post_close_integrity"
    assert e.calls[-1] == "publish"
    from app.services.daily_close_validation import validate_daily_close
    validate_daily_close(e.repo, e.day, rows=pl.read_parquet(e.paths["kline_daily_enriched"]))
    assert json.loads((e.repo.store.data_dir / "settlement_ready.json").read_text())["day"] == str(e.day)


@pytest.mark.parametrize("gate", ["before", "holiday", "unknown", "disabled", "worker"])
def test_1545_guards_do_not_create_or_write_jobs(integrity, monkeypatch, gate):
    e = integrity
    if gate == "before":
        e.clock[0] -= timedelta(seconds=1)
    elif gate in {"holiday", "unknown"}:
        monkeypatch.setattr(e.repair.trading_day, "is_trading_day",
                            lambda _: False if gate == "holiday" else None)
    elif gate == "disabled":
        monkeypatch.setattr(e.repair.preferences, "get_data_sync_auto_enabled", lambda: False)
    else:
        monkeypatch.setattr(settings, "service_role", "worker")
    assert e.repair.request_repair(e.repo, None) is None
    assert e.store.list_recent() == [] and not e.calls
    assert not (e.repo.store.data_dir / "post_close_integrity").exists()


def test_restart_catches_up_today_then_deduplicates_unchanged_inputs(integrity, monkeypatch):
    e = integrity
    e.clock[0] += timedelta(hours=2)
    before = {p: p.read_bytes() for p in e.paths.values()}
    first = e.repair.request_repair(e.repo, None)
    assert first["status"] == "succeeded"
    replacement = pipeline_jobs.JobStore(store_dir=e.store._store_dir)
    monkeypatch.setattr(pipeline_jobs, "job_store", replacement)
    assert e.repair.request_repair(e.repo, None)["job_id"] == first["job_id"]
    assert e.calls == ["missing", "publish"]
    assert all(p.read_bytes() == data for p, data in before.items())
    e.enriched.with_columns(pl.lit(8.).alias("raw_close")).write_parquet(e.paths["kline_daily_enriched"])
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: replacement.start(jid) or fn())
    assert e.repair.request_repair(e.repo, None)["attempts"] == 2
    assert e.calls.count("enriched") == 1


def test_busy_slot_and_active_job_wait_without_consuming_attempt(integrity):
    e = integrity
    other, _ = e.store.create(kind="daily_pipeline")
    e.store.start(other)
    assert pipeline_jobs.try_acquire_run_slot(other)
    pending = e.repair.request_repair(e.repo, None)
    assert pending["status"] == "pending" and pending["attempts"] == 0 and not e.calls
    pipeline_jobs.release_run_slot(other)
    assert e.repair.request_repair(e.repo, None)["status"] == "pending"
    e.store.fail(other, "15:35 unrelated remaining gaps")
    assert e.repair.request_repair(e.repo, None)["status"] == "succeeded"
    assert e.store.get(other)["status"] == "failed"


def test_unavailable_source_retries_after_backoff_without_false_readiness(integrity, monkeypatch):
    e = integrity
    e.raw.with_columns(pl.lit(0).alias("quote_ts"),
                       pl.lit("received").alias("quote_time_basis")).write_parquet(e.paths["kline_daily"])
    with monkeypatch.context() as patch:
        def unavailable(*a, **kw):
            raise RuntimeError("source unavailable")
        patch.setattr("app.services.kline_sync.sync_and_persist_daily_batch", unavailable)
        failed = e.repair.request_repair(e.repo, None)
        assert failed["status"] == "failed" and "source unavailable" in failed["error"]
        assert not (e.repo.store.data_dir / "settlement_ready.json").exists()
        assert e.repair.request_repair(e.repo, None)["attempts"] == 1
    e.clock[0] += timedelta(minutes=1)
    recovered = e.repair.request_repair(e.repo, None)
    assert recovered["status"] == "succeeded" and recovered["attempts"] == 2


@pytest.mark.parametrize("diagnostic", [
    {"remaining": 2}, {"gap_check_warning": "unknown pool"}, {"warning": "unverified gaps"},
])
def test_remaining_or_unknown_gaps_are_failed_and_preserved(integrity, monkeypatch, diagnostic):
    e = integrity
    monkeypatch.setattr("app.services.selection_daily_repair.run_repair_daily",
                        lambda *a, **kw: {"rows": 0, **diagnostic})
    failed = e.repair.request_repair(e.repo, None)
    assert failed["status"] == "failed" and failed["result"]["gap_check"] == {"rows": 0, **diagnostic}
    e.clock[0] += timedelta(minutes=1)
    assert e.repair.request_repair(e.repo, None)["attempts"] == 2
    assert e.calls.count("publish") == 1


def test_cancellation_stays_cancelled_and_never_publishes(integrity, monkeypatch):
    e = integrity
    def missing(*a, **kw):
        pipeline_jobs.request_cancel(e.store.active_id())
        return {"remaining": 0}
    monkeypatch.setattr("app.services.selection_daily_repair.run_repair_daily", missing)
    assert e.repair.request_repair(e.repo, None)["status"] == "cancelled"
    e.clock[0] += timedelta(minutes=10)
    assert e.repair.request_repair(e.repo, None)["status"] == "cancelled"
    assert not (e.repo.store.data_dir / "settlement_ready.json").exists()


def test_cross_day_during_collection_stops_before_publication(integrity, monkeypatch):
    e = integrity
    def missing(*a, **kw):
        e.clock[0] += timedelta(days=1)
        return {"remaining": 0}
    monkeypatch.setattr("app.services.selection_daily_repair.run_repair_daily", missing)
    assert e.repair.request_repair(e.repo, None)["status"] == "failed"
    assert not (e.repo.store.data_dir / "settlement_ready.json").exists()


def test_previous_pending_day_is_archived_without_historical_replay(integrity):
    e = integrity
    old = e.day - timedelta(days=1)
    e.repair._save(e.repo, {"day": str(old), "status": "pending", "attempts": 0})
    assert e.repair.request_repair(e.repo, None)["day"] == str(e.day)
    archived = json.loads((e.repo.store.data_dir / "post_close_integrity" / f"{old}.json").read_text())
    assert archived["status"] == "interrupted"
    assert e.calls == ["missing", "publish"]


def test_orphaned_job_recovers_and_state_read_is_side_effect_free(integrity, monkeypatch):
    e = integrity
    jid, _ = e.store.create(kind="post_close_integrity")
    e.store.start(jid)
    e.repair._save(e.repo, {"day": str(e.day), "status": "running", "job_id": jid, "attempts": 1})
    replacement = pipeline_jobs.JobStore(store_dir=e.store._store_dir)
    monkeypatch.setattr(pipeline_jobs, "job_store", replacement)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda job, fn: replacement.start(job) or fn())
    assert e.repair.summary(e.repo)["status"] == "interrupted"
    state = e.repair.request_repair(e.repo, None)
    assert state["status"] == "succeeded" and state["job_id"] != jid


def test_pipeline_api_exposes_persistent_summary_and_storage_failure(integrity):
    from app.api.pipeline import router
    e = integrity
    app = FastAPI()
    app.state.repo = e.repo
    app.state.scheduler = SimpleNamespace(get_job=lambda _: SimpleNamespace(next_run_time=e.clock[0]))
    app.include_router(router)
    with TestClient(app) as client:
        payload = client.get("/api/pipeline/jobs").json()
        assert payload["post_close_integrity"]["status"] == "not_run"
        assert payload["post_close_integrity"]["scheduled_time"] == "15:45"
        assert payload["post_close_integrity"]["schedule_registered"] is True
        e.repair.request_repair(e.repo, None)
        summary = client.get("/api/pipeline/jobs").json()["post_close_integrity"]
        assert summary["status"] == "succeeded" and summary["result"]["data_release_id"] == "release"
        assert "input_signature" not in summary
        (e.repo.store.data_dir / "post_close_integrity" / "current.json").write_text("bad json")
        summary = client.get("/api/pipeline/jobs").json()["post_close_integrity"]
        assert summary["available"] is False and summary["status"] == "unavailable"


def test_real_cron_and_retry_follow_auto_switch_and_live_capabilities(monkeypatch):
    from app.services import post_close_integrity as repair
    monkeypatch.setattr(daily_pipeline, "_register_auction_jobs", lambda *a: None)
    async def catalog(*a, **kw):
        pass
    monkeypatch.setattr(daily_pipeline, "run_catalog_job", catalog)
    auto = [True]
    monkeypatch.setattr(daily_pipeline._prefs, "get_data_sync_auto_enabled", lambda: auto[0])
    repo, original, live, quote = object(), CapabilitySet({}), CapabilitySet({}), object()
    monkeypatch.setattr(daily_pipeline, "_get_app_state",
                        lambda: SimpleNamespace(capabilities=live, quote_service=quote))
    calls = []
    monkeypatch.setattr(repair, "request_repair", lambda *a: calls.append(a))
    async def exercise():
        scheduler = daily_pipeline.start_scheduler(repo, original)
        try:
            cron = scheduler.get_job("post_close_integrity")
            assert cron is not None
            fire = cron.trigger.get_next_fire_time(None, datetime(2026, 11, 10, 15, tzinfo=CN_TZ))
            assert (fire.hour, fire.minute, str(fire.tzinfo)) == (15, 45, "Asia/Shanghai")
            retry = scheduler.get_job("post_close_integrity_retry")
            assert retry.trigger.interval.total_seconds() == 30
            cron.func()
            assert calls == [(repo, live, quote)]
            for enabled in (False, True):
                auto[0] = enabled
                daily_pipeline.apply_data_sync_mode(scheduler)
                assert (scheduler.get_job(cron.id).next_run_time is not None) == enabled
                assert (scheduler.get_job(retry.id).next_run_time is not None) == enabled
        finally:
            scheduler.shutdown(wait=False)
            await daily_pipeline.stop_catalog_jobs()
            await asyncio.sleep(0)
    asyncio.run(exercise())
