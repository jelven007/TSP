"""Cold history must bound wide intermediates without changing financial values."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.indicators import pipeline
from app.services.screener import ScreenerService


@pytest.fixture
def sample(tmp_path, monkeypatch):
    symbols = ["000001.SZ", "300001.SZ", "600000.SH", "688001.SH", "920001.BJ"]
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(140)]
    days = [day for day in days if day.weekday() < 5]
    rows = []
    for i, symbol in enumerate(symbols):
        for n, day in enumerate(days):
            # Uneven histories must still share the market's output date cutoff.
            if i == 0 and n >= len(days) - 10:
                continue
            close = 10 + i + n * .03 + (n % 7) * .1
            raw = close * (1.1 if n < 50 else 1)
            rows.append({
                "symbol": symbol, "date": day, "open": close - .1,
                "high": close + .3, "low": close - .3, "close": close,
                "volume": float(1000 + n * 7), "amount": raw * 1000,
                "raw_open": raw - .1, "raw_high": raw + .3,
                "raw_low": raw - .3, "raw_close": raw, "turnover_rate": 2.5,
            })
    frame = pl.DataFrame(rows).sort("symbol", "date")
    path = tmp_path / "kline_daily_enriched" / "part.parquet"
    path.parent.mkdir()
    frame.write_parquet(path)
    instruments = pl.DataFrame({
        "symbol": symbols, "name": ["stock", "ST stock", "stock", "stock", "stock"],
        "float_shares": [1000000.] * 5, "total_shares": [2000000.] * 5,
    })
    shares = pl.DataFrame({
        "symbol": [symbols[0]], "period_end": [days[0]],
        "announce_date": [days[30]], "float_shares": [500000.],
    })
    repo = MagicMock()
    repo.store.data_dir = tmp_path
    repo.get_enriched_history.return_value = None
    repo.get_instruments_asset.return_value = instruments
    repo.get_historical_shares.return_value = shares
    monkeypatch.setattr(pipeline, "_get_custom_signal_exprs", lambda: {})
    monkeypatch.setattr(pipeline, "_adaptive_sym_batch", lambda *args: 2)
    ScreenerService.clear_history_cache()
    yield SimpleNamespace(
        frame=frame, instruments=instruments, shares=shares, repo=repo,
        service=ScreenerService(repo), target=days[-1], days=days,
    )
    ScreenerService.clear_history_cache()


def expected_history(sample, lookback):
    columns = ["symbol", "date", "open", "high", "low", "close", "volume",
               "amount", "raw_open", "raw_close", "raw_high", "raw_low", "turnover_rate"]
    result = pipeline.compute_signals(pipeline.compute_indicators(sample.frame.select(columns)))
    if not sample.instruments.is_empty():
        result = pipeline.compute_limit_signals(
            result, sample.instruments, historical_shares=sample.shares,
        )
        result = result.join(
            sample.instruments.select("symbol", "name", "total_shares", "float_shares"),
            on="symbol", how="left",
        )
    dates = result["date"].unique().sort()
    cutoff = dates[-(lookback + 1)] if len(dates) > lookback else dates[0]
    return result.filter(pl.col("date") >= cutoff).sort("symbol", "date")


@pytest.mark.parametrize("with_instruments,lookback", [(True, 21), (False, 21), (True, 120)])
def test_cold_history_bounds_wide_compute_and_matches_original(sample, monkeypatch, with_instruments, lookback):
    if not with_instruments:
        sample.instruments = pl.DataFrame()
        sample.repo.get_instruments_asset.return_value = sample.instruments
    expected = expected_history(sample, lookback)
    compute = pipeline.compute_indicators

    def bounded(frame, *args, **kwargs):
        assert frame["symbol"].n_unique() <= 2, "cold history exceeded the wide batch budget"
        return compute(frame, *args, **kwargs)

    monkeypatch.setattr(pipeline, "compute_indicators", bounded)
    actual = sample.service._load_enriched_history(sample.target, lookback)
    assert_frame_equal(actual, expected, check_exact=True)
    assert actual["date"].n_unique() == min(lookback + 1, len(sample.days))
    monkeypatch.setattr(pipeline, "compute_indicators", lambda *_: pytest.fail("TTL cache missed"))
    assert_frame_equal(sample.service._load_enriched_history(sample.target, lookback), actual)


def test_custom_cross_section_signal_keeps_full_market_context(sample, monkeypatch):
    monkeypatch.setattr(pipeline, "_get_custom_signal_exprs", lambda: {
        "csg_cross_section": pl.col("close").rank().over("date") > 2,
        "csg_previous": pl.col("close") > pl.col("close").shift(1).over("symbol"),
    })
    expected = expected_history(sample, 21)
    actual = sample.service._load_enriched_history(sample.target, 21)
    assert_frame_equal(actual, expected, check_exact=True)
    assert actual["csg_cross_section"].any() and not actual["csg_cross_section"].all()


def test_legacy_limit_prices_use_global_latest_day_with_uneven_histories(sample, monkeypatch):
    sample.instruments = sample.instruments.with_columns(
        pl.lit(10000.).alias("limit_up"), pl.lit(1.).alias("limit_down"),
    )
    sample.repo.get_instruments_asset.return_value = sample.instruments
    monkeypatch.setattr(pipeline, "_adaptive_sym_batch", lambda *args: 1)
    expected = expected_history(sample, 21)
    actual = sample.service._load_enriched_history(sample.target, 21)
    assert_frame_equal(actual, expected, check_exact=True)


def test_failed_batch_does_not_cache_partial_history(sample, monkeypatch):
    compute = pipeline.compute_indicators
    calls = 0

    def fail_second(frame, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second batch failed")
        return compute(frame, *args, **kwargs)

    monkeypatch.setattr(pipeline, "compute_indicators", fail_second)
    with pytest.raises(RuntimeError, match="second batch failed"):
        sample.service._load_enriched_history(sample.target, 21)
    monkeypatch.setattr(pipeline, "compute_indicators", compute)
    assert_frame_equal(
        sample.service._load_enriched_history(sample.target, 21),
        expected_history(sample, 21), check_exact=True,
    )
