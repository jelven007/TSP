from __future__ import annotations

import json
from datetime import datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.second_quote_archive import build_snapshot_frame
from scripts.observe_second_acceptance import (
    Observer,
    arm,
    minute_coverage,
    minute_footer,
    next_candidate,
    write_json,
)

START = datetime(2026, 1, 5, 9, 30, tzinfo=CN_TZ)
SYMBOLS = ["000001.SZ", "600519.SH"]
PREFERENCES = {
    "second_quote_archive_enabled": True, "second_quote_archive_retention_days": 0,
    "realtime_quotes_enabled": True, "realtime_pull_stock": True,
    "realtime_quote_interval": 1.0, "minute_refresh_enabled": True,
    "minute_refresh_interval": 1, "realtime_data_provider": "rustdx",
    "full_minute_data_provider": "rustdx",
}


@pytest.fixture
def observer(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "user_data").mkdir(parents=True)
    write_json(data_dir / "user_data" / "preferences.json", PREFERENCES)
    (data_dir / "instruments").mkdir()
    pl.DataFrame({"symbol": [*SYMBOLS, "920001.BJ", "510300.SH"]}).write_parquet(
        data_dir / "instruments" / "instruments.parquet",
    )
    run_dir = tmp_path / "run"
    arm(run_dir, data_dir, START.date(), 1, START - timedelta(days=1))
    monkeypatch.setattr("scripts.observe_second_acceptance.health", lambda _: {"ok": True})
    return Observer(run_dir, "http://unused.test")


def write_quotes(observer, *, gap=False, missing_symbol=False):
    frames = []
    for i in range(61):
        if gap and i == 30:
            continue
        stamp = int((START + timedelta(seconds=i)).timestamp() * 1000)
        symbols = SYMBOLS[:1] if missing_symbol and i == 30 else SYMBOLS
        frames.append(build_snapshot_frame(
            {"stock": [
                {"symbol": symbol, "last_price": 10, "volume": 100 + i,
                 "amount": 100_000 + 1000 * i, "source_timestamp": stamp,
                 "provider_version": "1.12.2@test"}
                for symbol in symbols
            ]},
            provider="rustdx", capture_id=i + 1, collected_at_ms=stamp, trade_date=START.date(),
        ))
    path = observer.data_dir / "quote_second" / f"date={START.date()}" / "hour=09"
    path.mkdir(parents=True)
    pl.concat(frames).write_parquet(path / "part.parquet")
    write_json(observer.data_dir / "quote_second" / "session-test.json", {
        "last_flush_ms": stamp, "dropped_rounds": 0, "last_error": None,
        "last_write_ms": 10, "queue_peak": 2, "pending_rounds": 0, "queued_rounds": 0,
    })


def write_minutes(observer, *, stale=False, duplicate=False):
    observer.minute_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = START - timedelta(minutes=5) if stale else START
    rows = [*SYMBOLS, SYMBOLS[0]] if duplicate else SYMBOLS
    pl.DataFrame({"symbol": rows, "datetime": [stamp.replace(tzinfo=None)] * len(rows)}).write_parquet(
        observer.minute_path,
    )


def complete_window(observer):
    for i in range(5, 66, 5):
        observer.step(START + timedelta(seconds=i))
    observer.step(START + timedelta(seconds=80))
    return json.loads((observer.run_dir / "report.json").read_text())


def test_weekend_wait_does_not_read_or_create_market_data(observer):
    assert next_candidate(datetime(2026, 10, 11, 2, tzinfo=CN_TZ)).isoformat() == "2026-10-12"
    assert next_candidate(START + timedelta(minutes=1)).isoformat() == "2026-01-06"
    observer.step(START - timedelta(days=1))
    assert observer.record["status"] == "queued"
    assert not (observer.run_dir / "telemetry.jsonl").exists()
    assert not (observer.data_dir / "quote_second").exists()


def test_full_trading_day_profile_records_sessions_and_milestones(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "user_data").mkdir(parents=True)
    write_json(data_dir / "user_data" / "preferences.json", PREFERENCES)
    record = arm(
        tmp_path / "full-day",
        data_dir,
        START.date(),
        15,
        START - timedelta(days=1),
        profile="full-trading-day",
    )

    assert record["window_start"].endswith("09:15:00+08:00")
    assert record["window_end"].endswith("15:40:00+08:00")
    assert [
        (item["name"], item["start"][11:16], item["end"][11:16])
        for item in record["quality_segments"]
    ] == [
        ("morning", "09:30", "11:30"),
        ("afternoon", "13:00", "15:00"),
    ]
    assert record["minute_window_end"][11:16] == "15:00"
    assert set(record["milestones"]) == {
        "auction",
        "open",
        "morning_close",
        "afternoon_open",
        "close",
        "post_close",
    }


def test_complete_parallel_window_passes_with_fixed_universe_and_manifest(observer):
    observer.step(START)
    write_quotes(observer)
    write_minutes(observer)
    report = complete_window(observer)
    assert report["status"] == "succeeded"
    assert report["expected_symbols"] == 2
    assert report["quality"]["captures"] == 61
    assert report["interval_s"] == {"p50": 1.0, "p95": 1.0, "p99": 1.0}
    assert report["minute"]["coverage"] == 1
    assert report["observed_minute_file_updates"] == 1
    assert (observer.run_dir / "manifest.json").exists()
    assert (observer.run_dir / "report.md").exists()
    assert observer.record["finished_at"] is not None
    assert json.loads((observer.data_dir / "user_data" / "preferences.json").read_text()) == PREFERENCES


@pytest.mark.parametrize("issue", ["gap", "missing_symbol", "stale_minute", "duplicate_minute", "loss"])
def test_data_failures_are_not_hidden(observer, issue):
    observer.step(START)
    write_quotes(observer, gap=issue == "gap", missing_symbol=issue == "missing_symbol")
    write_minutes(observer, stale=issue == "stale_minute", duplicate=issue == "duplicate_minute")
    if issue == "loss":
        path = observer.data_dir / "quote_second" / "session-test.json"
        record = json.loads(path.read_text())
        record["dropped_rounds"] = 1
        write_json(path, record)
    report = complete_window(observer)
    assert report["status"] == "failed"
    expected = {
        "gap": "second_quote_quality", "missing_symbol": "second_quote_quality",
        "stale_minute": "minute_coverage_and_freshness",
        "duplicate_minute": "minute_keys_and_clock", "loss": "archive_no_reported_loss",
    }[issue]
    assert expected in report["failures"]


def test_missing_live_data_is_failure_not_inferred_holiday(observer):
    observer.step(START)
    report = complete_window(observer)
    assert report["status"] == "failed"
    assert report["quality"]["status"] == "missing"
    assert report["quality"]["missing_dates"] == ["2026-01-05"]
    assert not observer.record["trading_date_confirmed"]


def test_restart_and_expired_window_cannot_pass(observer):
    observer.step(START)
    restarted = Observer(observer.run_dir, "http://unused.test")
    restarted.recover(START + timedelta(seconds=10))
    assert restarted.record["status"] == "interrupted"
    restarted.step(START + timedelta(seconds=80))
    assert restarted.record["status"] == "interrupted"


def test_queued_task_recovers_but_missed_open_is_skipped(observer):
    restarted = Observer(observer.run_dir, "http://unused.test")
    restarted.recover(START - timedelta(minutes=10))
    assert restarted.record["status"] == "queued"
    restarted.step(START + timedelta(minutes=2))
    assert restarted.record["status"] == "skipped_prerequisite"


def test_late_observer_and_sleep_gap_fail_even_with_complete_market_files(observer):
    observer.step(START + timedelta(seconds=20))
    write_quotes(observer)
    write_minutes(observer)
    observer.step(START + timedelta(seconds=25))
    observer.step(START + timedelta(seconds=60))
    observer.step(START + timedelta(seconds=80))
    report = json.loads((observer.run_dir / "report.json").read_text())
    assert report["quality"]["status"] == "ready"
    assert "full_window_observed" in report["failures"]


def test_config_change_is_recorded_and_fails(observer):
    observer.step(START)
    write_quotes(observer)
    write_minutes(observer)
    write_json(observer.data_dir / "user_data" / "preferences.json", {
        **PREFERENCES, "minute_refresh_enabled": False,
    })
    report = complete_window(observer)
    assert "configuration_stable" in report["failures"]


def test_disabled_archive_blocks_start(observer):
    write_json(observer.data_dir / "user_data" / "preferences.json", {
        **PREFERENCES, "second_quote_archive_enabled": False,
    })
    observer.step(START)
    assert observer.record["status"] == "skipped_prerequisite"


def test_empty_minute_footer_and_future_clock_fail_closed(observer):
    observer.minute_path.parent.mkdir(parents=True)
    pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime}).write_parquet(observer.minute_path)
    footer = minute_footer(observer.minute_path)
    assert footer["rows"] == 0
    assert footer["latest"] is None
    pl.DataFrame({
        "symbol": SYMBOLS, "datetime": [(START + timedelta(minutes=3)).replace(tzinfo=None)] * 2,
    }).write_parquet(observer.minute_path)
    result = minute_coverage(observer.minute_path, SYMBOLS, START, START + timedelta(minutes=1))
    assert result["coverage"] == 0
    assert result["future_rows"] == 2


def test_existing_run_cannot_be_silently_rearmed(observer):
    with pytest.raises(FileExistsError):
        arm(observer.run_dir, observer.data_dir, START.date(), 1, START)
