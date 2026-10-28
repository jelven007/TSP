"""Bound protocol work for multi-day repairs while preserving exact missing keys."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import kline_gaps, kline_periods


def minute_rows(symbol, start, end):
    rows = []
    day = start
    while day <= end:
        if kline_gaps.market_time.published_trading_day(day) is True:
            rows.extend(
                {"symbol": symbol, "datetime": datetime.combine(day, datetime.min.time())
                 + timedelta(minutes=base + i), "open": 10., "high": 11., "low": 9.,
                 "close": 10., "volume": 1., "amount": 1000.}
                for base in (570, 780) for i in range(1, 121)
            )
        day += timedelta(days=1)
    return pl.from_dicts(rows) if rows else pl.DataFrame()


@pytest.mark.parametrize("available", [True, False])
def test_month_gap_repair_bounds_source_calls_and_reports_real_remaining(tmp_path, monkeypatch, available):
    symbols = ["000001.SZ", "600000.SH"]
    start, end = date(2026, 3, 30), date(2026, 4, 30)
    raw = pl.concat([minute_rows(symbol, start, end) for symbol in symbols])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: pl.DataFrame({"symbol": symbols}),
    )
    monkeypatch.setattr(
        kline_gaps.instrument_sync, "load_current_stock_pool",
        lambda *a, **kw: SimpleNamespace(symbols=symbols),
    )
    monkeypatch.setattr(kline_periods, "_period_provider", lambda _: (object(), "test"))
    monkeypatch.setattr(kline_periods, "refresh_period", lambda *_: None)
    monkeypatch.setattr(kline_gaps.market_time, "cn_now", lambda: datetime(2026, 10, 28, 16))
    calls = []

    def fetch(provider, period, batch, lo, hi, **kwargs):
        calls.append((batch, lo, hi))
        if not available:
            return pl.DataFrame()
        return raw.filter(pl.col("symbol").is_in(batch) & pl.col("datetime").is_between(lo, hi))

    monkeypatch.setattr(kline_periods, "fetch_period", fetch)
    kept = raw.filter(
        (pl.col("datetime").dt.hour() == 10) & (pl.col("datetime").dt.minute() == 10)
    ).with_columns(pl.lit(99.).alias("close"))
    kline_periods.persist_period(repo, "1m", kept)
    result = kline_gaps.sync_missing(repo, "1m", start, end)
    assert result["missing_before"] == raw.height - kept.height
    assert len(calls) <= 2  # one bounded 32-day window, at most one per 100 symbols
    assert all(len(batch) <= 100 and (hi.date() - lo.date()).days < 32 for batch, lo, hi in calls)
    stored = pl.read_parquet(list((tmp_path / "kline_minute").glob("date=*/*.parquet")))
    assert stored.join(kept.select("symbol", "datetime"), on=["symbol", "datetime"]).sort("datetime", "symbol").equals(
        kept.sort("datetime", "symbol")
    )
    if available:
        assert result["remaining"] == 0 and "error" not in result
        assert stored.height == raw.height
        calls.clear()
        repeat = kline_gaps.sync_missing(repo, "1m", start, end)
        assert repeat["requests"] == repeat["rows"] == 0 and not calls
    else:
        assert result["remaining"] == result["missing_before"]
        assert result["rows"] == 0 and "warning" in result and "error" not in result


def setup_repo(tmp_path, monkeypatch, symbols, listing_dates=None):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: pl.DataFrame(
            {"symbol": symbols, **({"listing_date": listing_dates} if listing_dates else {})}
        ),
    )
    monkeypatch.setattr(
        kline_gaps.instrument_sync, "load_current_stock_pool",
        lambda *a, **kw: SimpleNamespace(symbols=symbols),
    )
    monkeypatch.setattr(kline_periods, "_period_provider", lambda _: (object(), "test"))
    monkeypatch.setattr(kline_periods, "refresh_period", lambda *_: None)
    monkeypatch.setattr(kline_gaps.market_time, "cn_now", lambda: datetime(2026, 12, 31, 16))
    return repo


@pytest.mark.parametrize(("size", "span"), [(101, 65), (201, 364)])
def test_large_empty_source_bounds_memory_requests_and_progress(tmp_path, monkeypatch, size, span):
    symbols = [f"{i:06d}.SZ" for i in range(1, size + 1)]
    repo = setup_repo(tmp_path, monkeypatch, symbols)
    start, end = date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=span - 1)
    calls, progress = [], []

    def fetch(_, period, batch, lo, hi, **kwargs):
        calls.append((batch, lo, hi))
        return pl.DataFrame()

    monkeypatch.setattr(kline_periods, "fetch_period", fetch)
    result = kline_gaps.sync_missing(
        repo, "1m", start, end,
        on_progress=lambda _, pct, msg, **kw: progress.append((pct, kw["stage_pct"])),
    )
    days = kline_gaps._trading_days(start, end)
    assert result["missing_before"] == result["remaining"] == size * len(days) * 240
    assert result["rows"] == 0 and result["warning"] and "error" not in result
    grouped_days = {}
    for day in days:
        grouped_days.setdefault((day - start).days // 32, []).append(day)
    # A final single day retains separate morning/afternoon gap requests.
    windows = sum(1 if len(group) > 1 else 2 for group in grouped_days.values())
    assert len(calls) <= windows * ((size + 99) // 100)
    assert all(len(batch) <= 100 and (hi.date() - lo.date()).days < 32 for batch, lo, hi in calls)
    assert progress == sorted(progress)
    assert progress[-1] == (90, 100)


@pytest.mark.parametrize("outcome", ["partial", "failure", "concurrent", "cancel"])
def test_multiday_repair_outcomes_keep_exact_keys_and_existing_values(tmp_path, monkeypatch, outcome):
    from app.services.pipeline_jobs import JobCancelledError

    symbols = ["000001.SZ", "600000.SH"]
    start, end = date(2026, 4, 1), date(2026, 4, 2)
    repo = setup_repo(tmp_path, monkeypatch, symbols, [end.isoformat(), None])
    raw = pl.concat([minute_rows(symbol, start, end) for symbol in symbols])
    expected = raw.filter((pl.col("symbol") != symbols[0]) | (pl.col("datetime").dt.date() >= end))
    kept = expected.filter(pl.col("datetime").dt.minute() == 10).with_columns(pl.lit(99.).alias("close"))
    kline_periods.persist_period(repo, "1m", kept)
    gaps = expected.join(kept.select("symbol", "datetime"), on=["symbol", "datetime"], how="anti")
    calls = []

    def fetch(_, period, batch, lo, hi, **kwargs):
        calls.append((batch, lo, hi))
        if outcome == "cancel":
            raise JobCancelledError("用户取消")
        if outcome == "failure":
            raise RuntimeError("源请求失败")
        if outcome == "concurrent":
            kline_periods.persist_period(repo, "1m", gaps.with_columns(pl.lit(77.).alias("close")), only_missing=True)
        # Deliberately include pre-IPO and out-of-gap rows.
        return raw if outcome == "concurrent" else raw.filter(pl.col("datetime").dt.minute() % 2 == 0)

    monkeypatch.setattr(kline_periods, "fetch_period", fetch)
    if outcome == "cancel":
        with pytest.raises(JobCancelledError, match="用户取消"):
            kline_gaps.sync_missing(repo, "1m", start, end)
    else:
        result = kline_gaps.sync_missing(repo, "1m", start, end)
        assert result["missing_before"] == gaps.height
        if outcome == "concurrent":
            assert result["rows"] == result["remaining"] == 0 and "error" not in result
        elif outcome == "partial":
            assert result["rows"] + result["remaining"] == gaps.height
            assert 0 < result["remaining"] < gaps.height and result["warning"] and "error" not in result
        else:
            assert result["rows"] == 0 and result["remaining"] == gaps.height and result["error"]
    assert len(calls) == 1
    stored = pl.read_parquet(list((tmp_path / "kline_minute").glob("date=*/*.parquet")))
    assert stored.join(kept.select("symbol", "datetime"), on=["symbol", "datetime"]).sort("symbol", "datetime").equals(
        kept.sort("symbol", "datetime")
    )
    assert stored.join(expected.select("symbol", "datetime"), on=["symbol", "datetime"], how="anti").is_empty()
    if outcome == "concurrent":
        assert stored.join(gaps.select("symbol", "datetime"), on=["symbol", "datetime"])["close"].unique().to_list() == [77.]


@pytest.mark.parametrize("extra_first", [False, True])
def test_local_minute_repair_reads_additive_schema_and_monotone_batches(tmp_path, monkeypatch, extra_first):
    symbols = [f"{i:06d}.SZ" for i in range(1, 102)]
    repo = setup_repo(tmp_path, monkeypatch, symbols)
    day = date(2026, 4, 2)
    raw = pl.concat([minute_rows(symbol, day, day) for symbol in symbols])
    parts = raw.filter(pl.col("symbol") != symbols[-1]), raw.filter(pl.col("symbol") == symbols[-1])
    path = tmp_path / "kline_minute" / f"date={day}"
    path.mkdir(parents=True)
    for index, frame in enumerate(parts):
        if (index == 0) == extra_first:
            frame = frame.with_columns(pl.lit("received").alias("quote_time_basis"))
        frame.write_parquet(path / f"{index}.parquet")
    progress = []
    result = kline_gaps.sync_missing(
        repo, "5m", day, day,
        on_progress=lambda _, pct, msg, **kw: progress.append((pct, kw["stage_pct"])),
    )
    assert result["rows"] == len(symbols) * 48 and result["remaining"] == 0 and "error" not in result
    assert progress == sorted(progress)
    stored = pl.read_parquet(list((tmp_path / "kline_5minute").glob("date=*/*.parquet"))).sort("symbol", "datetime")
    assert stored.equals(kline_periods.aggregate_minute_period(raw, "5m", complete_only=True))
    progress.clear()
    full = kline_periods.sync_period(
        repo, "5m", datetime.combine(day, datetime.min.time()), datetime.combine(day, datetime.max.time()),
        symbols=symbols,
        on_progress=lambda _, pct, msg, **kw: progress.append((pct, kw["stage_pct"])),
    )
    assert full["rows"] == len(symbols) * 48 and "error" not in full
    assert progress == sorted(progress) and progress[-1] == (95, 100)
