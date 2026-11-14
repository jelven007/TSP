"""20:00 repairs source-day inputs before a next-session selection can freeze."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.services import pipeline_jobs, post_close_sync
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.services import selection_daily_repair as repair

    day = date(2026, 11, 10)
    clock = [datetime(2026, 11, 10, 20, tzinfo=CN_TZ)]
    repo = KlineRepository(DataStore(tmp_path))
    raw = pl.DataFrame({"symbol": ["000001.SZ"], "date": [day], "close": [10.],
                        "open": [10.], "high": [10.], "low": [10.],
                        "volume": [1.], "amount": [100.]})
    enriched = raw.with_columns(pl.col("close").alias("raw_close"))
    minute = pl.DataFrame({"symbol": ["000001.SZ"], "datetime": [datetime(2026, 11, 10, 15)],
                          "close": [10.]})
    paths = {}
    for folder, frame in (("kline_daily", raw), ("kline_daily_enriched", enriched),
                          ("kline_minute", minute)):
        path = tmp_path / folder / f"date={day}" / "part.parquet"
        path.parent.mkdir(parents=True)
        frame.write_parquet(path)
        paths[folder] = path
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    calls = []
    monkeypatch.setattr(settings, "service_role", "platform")
    monkeypatch.setattr(repair, "cn_now", lambda: clock[0])
    monkeypatch.setattr("app.jobs.daily_pipeline.cn_now", lambda: clock[0])
    monkeypatch.setattr(post_close_sync, "cn_now", lambda: clock[0])
    monkeypatch.setattr(repair.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: store.start(jid) or fn())
    monkeypatch.setattr(post_close_sync, "publish_data_release",
                        lambda *a, **kw: calls.append("publish") or {"release_id": "release"})

    def missing(*args, **kwargs):
        assert args[2:4] == (day, day)
        assert kwargs["period"] == "1d" and kwargs["mode"] == "missing"
        calls.append("missing")
        if not paths["kline_daily"].exists():
            raw.write_parquet(paths["kline_daily"])
        return {"rows": 0, "remaining": 0}

    def daily(symbols, repository, capset, **kwargs):
        assert symbols == ["000001.SZ"] and repository is repo
        assert kwargs["start_date"].date() == kwargs["end_date"].date() == day
        calls.append("daily")
        raw.write_parquet(paths["kline_daily"])
        return 1

    def fetch(provider, period, symbols, start, end, **kwargs):
        assert period == "1m" and symbols == ["000001.SZ"]
        assert start == end == datetime.combine(day, datetime.min.time().replace(hour=15))
        calls.append("minute")
        return minute

    def rebuild(**kwargs):
        assert kwargs["new_dates_only"]
        calls.append("enriched")
        paths["kline_daily_enriched"].parent.mkdir(exist_ok=True)
        enriched.write_parquet(paths["kline_daily_enriched"])
        return 1

    monkeypatch.setattr(repair, "run_repair_daily", missing)
    monkeypatch.setattr("app.services.kline_sync.sync_and_persist_daily_batch", daily)
    monkeypatch.setattr("app.services.kline_periods.require_period_provider", lambda *a: (None, "source"))
    monkeypatch.setattr("app.services.kline_periods.fetch_period", fetch)
    monkeypatch.setattr("app.services.kline_periods.refresh_period", lambda *a: None)
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", rebuild)
    monkeypatch.setattr(repo, "refresh_cache", lambda: None)
    pipeline_jobs.release_run_slot()
    yield SimpleNamespace(repair=repair, repo=repo, day=day, paths=paths, raw=raw, minute=minute,
                          enriched=enriched, store=store, clock=clock, calls=calls)
    pipeline_jobs.release_run_slot()
    repo.db.close()


@pytest.mark.parametrize("broken", ["daily_missing", "daily_price", "daily_clock", "minute_missing",
                                    "minute_price", "enriched_missing", "enriched_price"])
def test_bad_daily_inputs_are_corrected_and_only_then_published(env, broken):
    if broken.endswith("missing"):
        env.paths[broken.split("_")[0].replace("daily", "kline_daily").replace(
            "minute", "kline_minute").replace("enriched", "kline_daily_enriched")].unlink()
    else:
        folder = {"daily": "kline_daily", "minute": "kline_minute",
                  "enriched": "kline_daily_enriched"}[broken.split("_")[0]]
        path = env.paths[folder]
        frame = pl.read_parquet(path)
        if broken == "daily_clock":
            frame = frame.with_columns(pl.lit(0).alias("quote_ts"),
                                       pl.lit("received").alias("quote_time_basis"))
        else:
            frame = frame.with_columns(pl.lit(9.).alias("raw_close" if folder.endswith("enriched") else "close"))
        frame.write_parquet(path)
    state = env.repair.request_repair(env.repo, None)
    assert state["status"] == "succeeded"
    assert env.calls[-1] == "publish"
    assert json.loads((env.repo.store.data_dir / "settlement_ready.json").read_text())["day"] == str(env.day)
    from app.services.daily_close_validation import validate_daily_close
    validate_daily_close(env.repo, env.day, rows=pl.read_parquet(env.paths["kline_daily_enriched"]))
    assert ("daily" in env.calls) == (broken in {"daily_price", "daily_clock", "minute_missing", "minute_price"})


def test_normal_daily_is_not_refetched_and_success_survives_restart(env, monkeypatch):
    before = {p: p.read_bytes() for p in env.paths.values()}
    first = env.repair.request_repair(env.repo, None)
    assert first["status"] == "succeeded"
    assert "daily" not in env.calls and "minute" not in env.calls and "enriched" not in env.calls
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=env.store._store_dir))
    env.repair.request_repair(env.repo, None)
    assert env.calls.count("publish") == 1
    assert all(p.read_bytes() == data for p, data in before.items())


def test_halt_verification_precedes_missing_backfill_and_is_saved_with_release(env, monkeypatch):
    from app.services import selection_suspension_repair
    def verify(*a, **kw):
        env.calls.append("verify_halt")
        return {"added": 1, "remaining_symbols": [], "diagnostics": {}}
    monkeypatch.setattr(selection_suspension_repair, "repair_suspensions", verify)
    state = env.repair.request_repair(env.repo, None)
    assert env.calls[:2] == ["verify_halt", "missing"]
    assert state["result"]["suspension_check"]["added"] == 1


def test_residual_daily_gap_is_probed_again_then_rechecked_without_fake_prices(env, monkeypatch):
    from app.services import selection_suspension_repair
    checks = []
    monkeypatch.setattr(selection_suspension_repair, "repair_suspensions",
                        lambda *a, **kw: checks.append("verify") or
                        {"added": int(len(checks) == 2), "remaining_symbols": [], "diagnostics": {}})
    def missing(*a, **kw):
        env.calls.append("missing")
        return {"rows": 0, "remaining": int(len(checks) < 2)}
    monkeypatch.setattr(env.repair, "run_repair_daily", missing)
    state = env.repair.request_repair(env.repo, None)
    assert state["status"] == "succeeded"
    assert checks == ["verify", "verify"] and env.calls.count("missing") == 2


def test_source_failure_has_bounded_retry_then_recovers_without_fake_readiness(env, monkeypatch):
    path = env.paths["kline_daily"]
    env.raw.with_columns(pl.lit(0).alias("quote_ts"),
                         pl.lit("received").alias("quote_time_basis")).write_parquet(path)
    def unavailable(*a, **kw):
        raise RuntimeError("source unavailable")
    with monkeypatch.context() as patch:
        patch.setattr("app.services.kline_sync.sync_and_persist_daily_batch", unavailable)
        failed = env.repair.request_repair(env.repo, None)
        assert failed["status"] == "failed" and "source unavailable" in failed["error"]
        assert failed["next_retry_at"]
        assert not (env.repo.store.data_dir / "settlement_ready.json").exists()
        assert env.repair.request_repair(env.repo, None)["attempts"] == failed["attempts"]
    env.clock[0] += timedelta(minutes=1)
    done = env.repair.request_repair(env.repo, None)
    assert done["status"] == "succeeded" and done["attempts"] == 2


def test_changed_source_is_revalidated_even_with_an_old_ready_marker(env):
    assert env.repair.request_repair(env.repo, None)["status"] == "succeeded"
    env.enriched.with_columns(pl.lit(8.).alias("raw_close")).write_parquet(env.paths["kline_daily_enriched"])
    assert env.repair.request_repair(env.repo, None)["status"] == "succeeded"
    assert env.calls.count("publish") == 2 and env.calls.count("enriched") == 1


def test_busy_writer_waits_and_terminal_five_period_failure_does_not_block(env):
    other, _ = env.store.create(kind="daily_pipeline")
    env.store.start(other)
    assert pipeline_jobs.try_acquire_run_slot(other)
    assert env.repair.request_repair(env.repo, None)["status"] == "pending"
    assert not env.calls
    env.store.fail(other, "unrelated 11:30 minute gap")
    pipeline_jobs.release_run_slot(other)
    assert env.repair.request_repair(env.repo, None)["status"] == "succeeded"
    assert env.store.get(other)["status"] == "failed"


def test_cancelled_repair_is_not_resurrected_by_the_retry_timer(env, monkeypatch):
    def missing(*a, **kw):
        pipeline_jobs.request_cancel(env.store.active_id())
        return {"rows": 0}
    monkeypatch.setattr(env.repair, "run_repair_daily", missing)
    assert env.repair.request_repair(env.repo, None)["status"] == "cancelled"
    env.clock[0] += timedelta(minutes=10)
    assert env.repair.request_repair(env.repo, None)["status"] == "cancelled"
    assert not (env.repo.store.data_dir / "settlement_ready.json").exists()


@pytest.mark.parametrize("when", [datetime(2026, 11, 10, 19, 59, 59, tzinfo=CN_TZ),
                                 datetime(2026, 11, 11, 9, 30, tzinfo=CN_TZ)])
def test_outside_latest_postclose_window_never_repairs_history(env, when):
    env.clock[0] = when
    assert env.repair.request_repair(env.repo, None) is None
    assert not env.calls


def test_disabled_auto_mode_preserves_pending_request(env, monkeypatch):
    assert pipeline_jobs.try_acquire_run_slot("other")
    env.repair.request_repair(env.repo, None)
    pipeline_jobs.release_run_slot("other")
    monkeypatch.setattr(env.repair.preferences, "get_data_sync_auto_enabled", lambda: False)
    assert env.repair.request_repair(env.repo, None) is None
    assert not env.calls


def test_wrong_source_auction_price_is_not_written_to_manufacture_agreement(env, monkeypatch):
    env.minute.with_columns(pl.lit(9.).alias("close")).write_parquet(env.paths["kline_minute"])
    original = env.paths["kline_minute"].read_bytes()
    monkeypatch.setattr("app.services.kline_periods.fetch_period", lambda *a, **kw:
                        env.minute.with_columns(pl.lit(8.).alias("close")))
    assert env.repair.request_repair(env.repo, None)["status"] == "failed"
    assert env.paths["kline_minute"].read_bytes() == original
    assert not (env.repo.store.data_dir / "settlement_ready.json").exists()


def test_orphaned_attempt_rechecks_inputs_and_does_not_stay_running_forever(env, monkeypatch):
    job_id, _ = env.store.create(kind="daily_pipeline")
    env.store.start(job_id)
    env.repair._save(env.repo, {"day": str(env.day), "as_of": "2026-11-11",
                               "status": "running", "job_id": job_id, "attempts": 1})
    replacement = pipeline_jobs.JobStore(store_dir=env.store._store_dir)
    monkeypatch.setattr(pipeline_jobs, "job_store", replacement)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: replacement.start(jid) or fn())
    state = env.repair.request_repair(env.repo, None)
    assert state["status"] == "succeeded" and state["job_id"] != job_id
    assert replacement.get(job_id)["status"] == "interrupted"


def test_previous_source_day_can_recover_before_target_open(env):
    env.clock[0] = datetime(2026, 11, 11, 8, tzinfo=CN_TZ)
    state = env.repair.request_repair(env.repo, None)
    assert state["status"] == "succeeded"
    assert state["day"] == "2026-11-10" and state["as_of"] == "2026-11-11"


def test_unrelated_gap_diagnostics_are_preserved_when_daily_close_is_valid(env, monkeypatch):
    monkeypatch.setattr(env.repair, "run_repair_daily", lambda *a, **kw:
                        {"rows": 0, "remaining": 2, "warning": "out-of-scope missing"})
    state = env.repair.request_repair(env.repo, None)
    assert state["status"] == "succeeded" and state["result"]["gap_check"]["remaining"] == 2
    assert state["retry_required"] is True and state["warning"]
    assert state["error"] is None
    assert env.store.get(state["job_id"])["status"] == "succeeded"
    assert (env.repo.store.data_dir / "settlement_ready.json").exists()
    env.clock[0] += timedelta(minutes=1)
    assert env.repair.request_repair(env.repo, None)["attempts"] == 2
    assert env.calls.count("publish") == 1


def test_no_daily_rows_waits_for_backfill_without_failed_task_or_fake_ready(env, monkeypatch):
    env.paths["kline_daily"].unlink()
    monkeypatch.setattr(env.repair, "run_repair_daily", lambda *a, **kw:
                        {"rows": 0, "remaining": 1})
    state = env.repair.request_repair(env.repo, None)
    assert state["status"] == "succeeded" and state["error"] is None
    assert state["retry_required"] is True
    assert state["result"]["selection_ready"] is False
    assert not (env.repo.store.data_dir / "settlement_ready.json").exists()
    assert "publish" not in env.calls
