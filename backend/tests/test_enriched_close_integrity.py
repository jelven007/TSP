"""Completed derived rows must not escape the daily self-repair scan."""
from datetime import date, datetime, time
from types import SimpleNamespace

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.market_time import CN_TZ
from app.services import data_integrity, instrument_sync, kline_sync

DAY = date(2026, 11, 9)
TODAY = date(2026, 11, 11)


def ms(day=DAY, hour=15):
    return int(datetime.combine(day, time(hour), CN_TZ).timestamp() * 1000)


def write(root, table, day, *, basis=None, stamp=None, volume=100.):
    out = root / table / f"date={day}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["000001.SZ"], "date": [day], "close": [11.89],
        "raw_close": [11.89], "volume": [volume], "amount": [volume * 11.89],
        "quote_ts": pl.Series([stamp], dtype=pl.Int64),
        "quote_time_basis": pl.Series([basis], dtype=pl.String),
    }).write_parquet(out)


@pytest.mark.parametrize("basis,stamp,volume", [
    ("received", ms(), 100.),
    ("source_event", ms(), 100.),
    ("source_snapshot", ms(hour=14), 100.),
    ("source_snapshot", ms(TODAY), 100.),
    ("source_snapshot", None, 100.),
    ("received", ms(), 0.),
])
def test_final_raw_does_not_hide_unfinalized_enriched(tmp_path, monkeypatch, basis, stamp, volume):
    monkeypatch.setattr(data_integrity, "_candidate_days", lambda *a: [DAY])
    write(tmp_path, "kline_daily", DAY)
    write(tmp_path, "kline_daily_enriched", DAY, basis=basis, stamp=stamp, volume=volume)
    issues = data_integrity.scan_recent_integrity(tmp_path, today=TODAY)
    assert [(i.day, i.table, i.kind) for i in issues] == [
        (DAY, "kline_daily_enriched", "snapshot"),
    ]


@pytest.mark.parametrize("basis,stamp", [(None, None), ("source_snapshot", ms())])
def test_final_enriched_and_current_provisional_rows_are_not_flagged(tmp_path, monkeypatch, basis, stamp):
    monkeypatch.setattr(data_integrity, "_candidate_days", lambda *a: [DAY])
    write(tmp_path, "kline_daily", DAY)
    write(tmp_path, "kline_daily_enriched", DAY, basis=basis, stamp=stamp)
    write(tmp_path, "kline_daily_enriched", TODAY, basis="received", stamp=ms(TODAY))
    assert data_integrity.scan_recent_integrity(tmp_path, today=TODAY) == []


def test_pipeline_repairs_enriched_only_issue_instead_of_refreshing_today(tmp_path, monkeypatch):
    captured = []
    issue = data_integrity.IntegrityIssue(DAY, "kline_daily_enriched", "snapshot")
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *a, **kw: [issue])
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: TODAY)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(instrument_sync, "load_current_stock_pool", lambda *a, **kw: SimpleNamespace(
        symbols=("000001.SZ",), as_of=TODAY, source="test", boards=("深主板",),
    ))
    class StopAfterSyncError(Exception):
        pass

    def sync(*a, **kw):
        captured.append(kw["start_date"].date())
        raise StopAfterSyncError()

    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", sync)
    monkeypatch.setattr(kline_sync, "sync_daily_by_quotes", lambda *a: pytest.fail("stale history must be repaired"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), latest_daily_date=lambda: TODAY)
    with pytest.raises(StopAfterSyncError):
        daily_pipeline.run_now(repo, SimpleNamespace(has=lambda *a: True))
    assert captured == [DAY]
