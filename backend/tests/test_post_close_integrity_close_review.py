"""Closing-price repairs must respect request order and retain per-stock evidence."""
import polars as pl
import pytest
from test_post_close_integrity import integrity as integrity
from test_selection_daily_repair import env as env

from app.services import kline_sync
from app.services.minute_write_order import begin_minute_request


def test_review_replaces_protected_bad_close_with_a_later_verified_request(integrity):
    e = integrity
    bad = e.minute.with_columns(pl.lit(9.).alias("close"))
    sequence = begin_minute_request(e.repo.store.data_dir / "kline_minute")
    kline_sync._write_minute_partition(
        bad, e.repo.store.data_dir / "kline_minute", request_seq=sequence)
    state = e.repair.request_repair(e.repo, None)
    assert state["status"] == "succeeded"
    assert pl.read_parquet(e.paths["kline_minute"])["close"].to_list() == [10.]
    assert state["result"]["close_review"]["before"]["invalid_count"] == 1
    assert state["result"]["close_review"]["after"]["invalid_count"] == 0


def test_failed_review_retains_every_bad_stock_and_actual_times(integrity, monkeypatch):
    e = integrity
    symbols = ["000001.SZ", "600000.SH", "600001.SH"]
    raw = pl.concat([
        e.raw.with_columns(pl.lit(symbol).alias("symbol"), pl.lit(price).alias("close"))
        for symbol, price in zip(symbols, [10., 12., 14.], strict=True)
    ])
    raw.write_parquet(e.paths["kline_daily"])
    raw.with_columns(pl.col("close").alias("raw_close")).write_parquet(e.paths["kline_daily_enriched"])
    minutes = pl.concat([
        e.minute.with_columns(pl.lit(symbol).alias("symbol"), pl.lit(price).alias("close"))
        for symbol, price in zip(symbols, [9., 11., 14.], strict=True)
    ])
    minutes.write_parquet(e.paths["kline_minute"])
    monkeypatch.setattr("app.services.kline_sync.sync_and_persist_daily_batch", lambda *a, **kw: 0)
    monkeypatch.setattr("app.services.kline_periods.fetch_period", lambda *a, **kw: minutes.head(0))
    state = e.repair.request_repair(e.repo, None)
    assert state["status"] == "failed"
    after = state["diagnostics"]["close_review"]["after"]
    assert after["checked_count"] == 3 and after["invalid_count"] == 2
    assert after["valid_count"] == 1
    assert {item["symbol"] for item in after["errors"]} == set(symbols[:2])
    first = next(item for item in after["errors"] if item["symbol"] == symbols[0])
    assert first["daily_closes"] == [10.] and first["minute_closes"] == [9.]
    assert state["requested_at"] and state["first_started_at"] and state["last_finished_at"]
    assert state["last_stage"]
    summary = e.repair.summary(e.repo)
    assert summary["diagnostics"]["close_review"] == state["diagnostics"]["close_review"]
    assert not (e.repo.store.data_dir / "settlement_ready.json").exists()


@pytest.mark.parametrize("problem", ["duplicate_daily", "invalid_clock", "missing_minute", "enriched_price"])
def test_review_records_related_close_errors_before_repair(integrity, monkeypatch, problem):
    e = integrity
    if problem == "duplicate_daily":
        pl.concat([e.raw, e.raw]).write_parquet(e.paths["kline_daily"])
    elif problem == "invalid_clock":
        e.raw.with_columns(pl.lit(0).alias("quote_ts"),
                          pl.lit("received").alias("quote_time_basis")).write_parquet(e.paths["kline_daily"])
    elif problem == "missing_minute":
        e.paths["kline_minute"].unlink()
    else:
        e.enriched.with_columns(pl.lit(8.).alias("raw_close")).write_parquet(e.paths["kline_daily_enriched"])
    # Keep the malformed evidence unchanged; the task must fail with diagnostics.
    monkeypatch.setattr(e.repair, "repair_inputs", lambda *a, **kw: (_ for _ in ()).throw(ValueError("repair unavailable")))
    state = e.repair.request_repair(e.repo, None)
    assert state["status"] == "failed"
    before = state["diagnostics"]["close_review"]["before"]
    assert before["invalid_count"] == 1
    assert before["errors"][0]["symbol"] == "000001.SZ"
