"""Candidate exits follow market sessions, including missing security bars."""
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig

INDEPENDENT = ["simulate_independent_candidates", "simulate_independent_candidates_legacy"]
METHODS = [*INDEPENDENT, "simulate_portfolio", "simulate_portfolio_legacy"]


def _panel(asset_days=None, *, low_day=None):
    asset_days = range(6) if asset_days is None else asset_days
    return pl.DataFrame([
        {
            "symbol": symbol, "name": symbol, "date": date(2024, 1, 2) + timedelta(days=i),
            "open": 10.0, "high": 10.1,
            "low": 8.8 if i == low_day and symbol == "600001.SH" else 9.9,
            "close": 10.0, "raw_close": 10.0, "volume": 10_000.0,
        }
        for symbol, days in (("600001.SH", asset_days), ("600002.SH", range(6)))
        for i in days
    ]).sort(["symbol", "date"])


def _run(method, frame, *, sell_day=None, **kwargs):
    return getattr(BacktestEngine(None), method)(
        frame,
        frame.select((pl.col("symbol") == "600001.SH") & (pl.col("date") == date(2024, 1, 2))).to_series(),
        frame.select(
            (pl.col("symbol") == "600001.SH")
            & (pl.col("date") == (date(2024, 1, 2) + timedelta(days=sell_day or 50)))
        ).to_series(),
        MatcherConfig(matching="open_t+1", fees_pct=0, slippage_bps=0, **kwargs),
    )


@pytest.mark.parametrize("method", INDEPENDENT)
def test_last_available_security_bar_is_not_the_market_window_end(method):
    result = _run(method, _panel(range(3)))
    assert result.trades == []
    assert result.stats["unclosed_candidates"] == 1
    assert result.stats["unclosed_positions"][0]["mark_date"] == "2024-01-04"


@pytest.mark.parametrize("method", INDEPENDENT)
def test_no_future_security_bars_retains_the_open_position(method):
    result = _run(method, _panel(range(2)))
    assert result.trades == []
    assert result.stats["unclosed_candidates"] == 1
    assert result.stats["unclosed_positions"][0]["mark_date"] == "2024-01-03"


@pytest.mark.parametrize("method", ["simulate_portfolio", "simulate_portfolio_legacy"])
def test_portfolio_without_future_security_bars_exposes_unclosed_equity(method):
    from app.backtest.strategy import BacktestResultPolicy
    from app.backtest.walkforward import aggregate_oos

    result = _run(method, _panel(range(2)))
    assert result.trades == []
    assert result.stats["unclosed_positions"] == 1
    assert result.stats["unrealized_pnl_amount"] == 0
    selected = BacktestResultPolicy.optimizer_trial("total_return").select_stats(result.stats)
    assert selected["unclosed_positions"] == 1
    summary = aggregate_oos([{
        "index": 0, "test_start": "2024-01-02", "test_end": "2024-01-07",
        "is_score": 0, "oos_objective": 0, "oos_stats": selected,
    }], "total_return")
    assert summary["compounded_oos_return"] is None
    assert summary["aggregation_unavailable_reason"] == "unclosed_boundary_positions"


@pytest.mark.parametrize("method", INDEPENDENT)
def test_max_hold_counts_market_sessions_across_suspension(method):
    result = _run(method, _panel([0, 1, 3, 4, 5]), max_hold_days=2)
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-05"
    assert trade.duration == 2
    assert trade.exit_reason == "max_hold"


@pytest.mark.parametrize("method", METHODS)
def test_scheduled_open_exit_never_uses_later_intraday_stop(method):
    result = _run(method, _panel(low_day=3), sell_day=2, stop_loss_pct=0.06)
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-05"
    assert trade.exit_price == 10.0
    assert trade.exit_reason == "signal"
