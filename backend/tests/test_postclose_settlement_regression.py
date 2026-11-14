"""Next-session readiness depends on validated daily inputs, not unrelated gaps."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.services import pipeline_jobs, post_close_sync
from app.services.quote_service import QuoteService
from app.tickflow.repository import DataStore, KlineRepository


@pytest.mark.parametrize("builder", ["_build_daily", "_build_quote_extra", "_build_index_quotes"])
def test_boundary_snapshot_accepts_late_optional_time_fields(builder):
    records = [{"symbol": "000001.SZ", "last_price": 10., "open": 10., "high": 10.,
                "low": 10., "volume": 1., "amount": 100., "prev_close": 10.,
                "quote_time": None} for _ in range(100)]
    records.append({**records[0], "quote_time": "15:00:00.006"})
    assert getattr(QuoteService, builder)(records).height == 101


@pytest.fixture
def settlement(tmp_path, monkeypatch):
    day = date(2026, 11, 10)
    now = datetime(2026, 11, 10, 15, 35, tzinfo=CN_TZ)
    repo = KlineRepository(DataStore(tmp_path))
    raw = pl.DataFrame({"symbol": ["000001.SZ"], "date": [day], "close": [10.],
                        "open": [10.], "high": [10.], "low": [10.],
                        "volume": [1.], "amount": [100.]})
    enriched = raw.with_columns(pl.col("close").alias("raw_close"))
    for folder, frame in (("kline_daily", raw), ("kline_daily_enriched", enriched)):
        target = tmp_path / folder / f"date={day}"
        target.mkdir(parents=True)
        frame.write_parquet(target / "part.parquet")
    target = tmp_path / "kline_minute" / f"date={day}"
    target.mkdir(parents=True)
    pl.DataFrame({"symbol": ["000001.SZ"], "datetime": [datetime(2026, 11, 10, 15)],
                  "close": [10.]}).write_parquet(target / "part.parquet")
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(settings, "service_role", "platform")
    monkeypatch.setattr(post_close_sync, "cn_now", lambda: now)
    monkeypatch.setattr("app.jobs.daily_pipeline.cn_now", lambda: now)
    monkeypatch.setattr(post_close_sync.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(post_close_sync.trading_day, "is_trading_day", lambda _: True)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: store.start(jid) or fn())
    monkeypatch.setattr(post_close_sync, "publish_data_release", lambda *a, **kw: {"release_id": "release"})
    monkeypatch.setattr(post_close_sync, "run_repair_daily", lambda *a, **kw: {"rows": 0})
    monkeypatch.setattr("app.services.kline_sync.sync_and_persist_daily_batch",
                        lambda *a, **kw: pytest.fail("valid daily rows must not be refetched"))
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline",
                        lambda *a, **kw: pytest.fail("valid enriched rows must not be rewritten"))
    pipeline_jobs.release_run_slot()
    yield SimpleNamespace(repo=repo, day=day, store=store)
    pipeline_jobs.release_run_slot()
    repo.db.close()


def test_complete_postclose_day_publishes_verified_settlement_marker(settlement):
    before = {p: p.read_bytes() for p in settlement.repo.store.data_dir.glob("kline_*/date=*/*.parquet")}
    state = post_close_sync.request_sync(settlement.repo, None)
    assert state["status"] == "succeeded"
    marker = json.loads((settlement.repo.store.data_dir / "settlement_ready.json").read_text())
    assert marker == {"day": str(settlement.day), "release_id": "release"}
    assert all(p.read_bytes() == value for p, value in before.items())


def test_unrelated_minute_gap_retains_failure_but_does_not_block_valid_daily(settlement, monkeypatch):
    def repair(*a, **kw):
        return {"remaining": 1, "warning": "11:30 missing"} if kw["period"] == "1m" else {"rows": 0}
    monkeypatch.setattr(post_close_sync, "run_repair_daily", repair)
    state = post_close_sync.request_sync(settlement.repo, None)
    assert state["status"] == "failed" and state["periods"]["1m"]["remaining"] == 1
    marker = json.loads((settlement.repo.store.data_dir / "settlement_ready.json").read_text())
    assert marker["day"] == str(settlement.day)


def test_invalid_closing_price_cannot_publish_readiness(settlement):
    target = settlement.repo.store.data_dir / "kline_minute" / f"date={settlement.day}" / "part.parquet"
    pl.read_parquet(target).with_columns(pl.lit(9.).alias("close")).write_parquet(target)
    state = post_close_sync.request_sync(settlement.repo, None)
    assert state["status"] == "failed"
    assert "收盘" in state["error"]
    assert not (settlement.repo.store.data_dir / "settlement_ready.json").exists()


def test_provisional_daily_is_repaired_without_refetching_completed_rows(settlement, monkeypatch):
    target = settlement.repo.store.data_dir / "kline_daily" / f"date={settlement.day}" / "part.parquet"
    raw = pl.read_parquet(target)
    raw.with_columns(pl.lit(0).alias("quote_ts"),
                     pl.lit("received").alias("quote_time_basis")).write_parquet(target)
    calls = []

    def repair(symbols, repo, capset, **kwargs):
        calls.append((symbols, kwargs["start_date"].date(), kwargs["end_date"].date()))
        raw.write_parquet(target)
        return raw.height

    monkeypatch.setattr("app.services.kline_sync.sync_and_persist_daily_batch", repair)
    assert post_close_sync.request_sync(settlement.repo, None)["status"] == "succeeded"
    assert calls == [(["000001.SZ"], settlement.day, settlement.day)]


@pytest.mark.parametrize("missing", [False, True])
def test_stale_or_missing_enriched_is_rebuilt_only_for_target_day(settlement, monkeypatch, missing):
    target = settlement.repo.store.data_dir / "kline_daily_enriched" / f"date={settlement.day}" / "part.parquet"
    correct = pl.read_parquet(target)
    if missing:
        target.unlink()
    else:
        correct.with_columns(pl.lit(9.).alias("raw_close")).write_parquet(target)
    calls = []

    def rebuild(**kwargs):
        assert kwargs["new_dates_only"] is True
        assert not list(target.parent.glob("*.parquet"))
        calls.append(kwargs["data_dir"])
        target.parent.mkdir(exist_ok=True)
        correct.write_parquet(target)
        return correct.height

    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", rebuild)
    monkeypatch.setattr(settlement.repo, "refresh_cache", lambda: None)
    assert post_close_sync.request_sync(settlement.repo, None)["status"] == "succeeded"
    assert calls == [settlement.repo.store.data_dir]


def test_cancel_during_daily_correction_never_marks_ready(settlement, monkeypatch):
    target = settlement.repo.store.data_dir / "kline_daily" / f"date={settlement.day}" / "part.parquet"
    pl.read_parquet(target).with_columns(pl.lit(0).alias("quote_ts"),
                                        pl.lit("received").alias("quote_time_basis")).write_parquet(target)

    def repair(*a, **kwargs):
        pipeline_jobs.request_cancel(settlement.store.active_id())
        return 0

    monkeypatch.setattr("app.services.kline_sync.sync_and_persist_daily_batch", repair)
    state = post_close_sync.request_sync(settlement.repo, None)
    assert state["status"] == "cancelled"
    assert not (settlement.repo.store.data_dir / "settlement_ready.json").exists()
