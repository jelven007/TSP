"""Portfolio resources can only be reused after the actual sale phase."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import (
    build_market_data_matrix,
    build_market_matrix_from_signals,
    make_signal_matrix,
)

METHODS = ["simulate_portfolio", "simulate_portfolio_legacy"]


def _run(method, *, gap=False, sell_at_close=False, buy_at_close=False, max_positions=1, **config):
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(5)]
    frame = pl.DataFrame([
        {
            "symbol": symbol, "name": symbol, "date": day,
            "open": 9.3 if gap and i == 2 and symbol == "600001.SH" else 10.,
            "high": 10.1,
            "low": 9.2 if i == 2 and symbol == "600001.SH" else 9.9,
            "close": 10., "raw_close": 10., "volume": 100_000.,
        }
        for symbol in ("600001.SH", "600002.SH")
        for i, day in enumerate(days)
    ])
    delay = 0 if buy_at_close else 1
    entry = frame.select(
        ((pl.col("symbol") == "600001.SH") & (pl.col("date") == days[1 - delay]))
        | ((pl.col("symbol") == "600002.SH") & (pl.col("date") == days[2 - delay]))
    ).to_series()
    exit_signal = frame.select(
        pl.lit(sell_at_close) & (pl.col("symbol") == "600001.SH")
        & (pl.col("date") == days[2])
    ).to_series()
    return getattr(BacktestEngine(None), method)(
        frame, entry, exit_signal,
        MatcherConfig(
            matching="open_t+1", fees_pct=0, slippage_bps=0,
            entry_fill="close_t" if buy_at_close else "open_t+1",
            exit_fill="close_t" if sell_at_close else "open_t+1",
            max_positions=max_positions, initial_capital=100_000,
            **config,
        ),
    )


@pytest.mark.parametrize("method", METHODS)
def test_intraday_stop_cannot_release_slot_for_same_day_open_buy(method):
    result = _run(method, stop_loss_pct=.06)
    assert [t.symbol for t in result.trades] == ["600001.SH"]
    assert result.trades[0].exit_price == pytest.approx(9.4)
    assert result.stats["execution"]["buy_no_slot"] == 1


@pytest.mark.parametrize("method", METHODS)
def test_close_sale_cannot_fund_same_day_open_buy(method):
    result = _run(method, sell_at_close=True)
    assert [t.symbol for t in result.trades] == ["600001.SH"]
    assert result.stats["execution"]["buy_no_slot"] == 1


@pytest.mark.parametrize("method", METHODS)
def test_open_gap_exit_releases_resources_for_open_buy(method):
    result = _run(method, gap=True, stop_loss_pct=.06)
    assert [t.symbol for t in result.trades] == ["600001.SH", "600002.SH"]
    assert result.trades[0].exit_price == pytest.approx(9.3)
    assert result.trades[1].entry_date == "2024-01-04"


@pytest.mark.parametrize("method", METHODS)
def test_intraday_exit_can_release_resources_for_close_buy(method):
    result = _run(method, buy_at_close=True, stop_loss_pct=.06)
    assert [t.symbol for t in result.trades] == ["600001.SH", "600002.SH"]
    assert result.trades[1].entry_date == "2024-01-04"


@pytest.mark.parametrize("method", METHODS)
def test_open_buy_size_is_not_reduced_by_a_future_intraday_loss(method):
    result = _run(method, max_positions=2, stop_loss_pct=.06)
    second = next(t for t in result.trades if t.symbol == "600002.SH")
    assert second.raw_entry_shares == 5000
    assert second.entry_value == 50_000


@pytest.mark.parametrize("method", [
    *METHODS, "simulate_independent_candidates", "simulate_independent_candidates_legacy",
])
def test_close_entry_does_not_inherit_the_earlier_daily_peak(method):
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(4)]
    frame = pl.DataFrame({
        "symbol": ["600001.SH"] * 4, "date": days,
        "open": [10.] * 4, "close": [10.] * 4, "raw_close": [10.] * 4,
        "high": [11., 10., 10., 10.], "low": [10.] * 4, "volume": [10000.] * 4,
    })
    result = getattr(BacktestEngine(None), method)(
        frame, pl.Series([True, False, False, False]), pl.Series([False] * 4),
        MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, trailing_stop_pct=.06),
    )
    assert result.trades[0].exit_reason == "end"


def test_minute_price_override_reserves_resources_before_unknown_intraday_exits():
    frame = pl.DataFrame([
        {
            "symbol": symbol, "date": date(2024, 1, 2) + timedelta(days=i),
            "open": 10., "high": 10.1, "close": 10., "raw_close": 10., "volume": 10000.,
            "low": 9.2 if i == 2 and symbol == "600001.SH" else 9.9,
        }
        for symbol in ("600001.SH", "600002.SH") for i in range(5)
    ])
    market = build_market_data_matrix(frame)
    entry = np.zeros(market.shape, dtype=np.uint8)
    entry[1, 0] = entry[2, 1] = 1
    prices = np.where(entry, 10., np.nan)
    execution = build_market_matrix_from_signals(
        market, make_signal_matrix(market.shape, entry=entry), entry_price_override=prices,
    )
    result = BacktestEngine(None).simulate_market_matrix(
        execution, MatcherConfig(matching="close_t", max_positions=1, stop_loss_pct=.06),
    )
    assert [t.symbol for t in result.trades] == ["600001.SH"]
    assert result.stats["execution"]["buy_no_slot"] == 1
