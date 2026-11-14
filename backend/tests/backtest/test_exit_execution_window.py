"""Minute signal orders expire after five trading minutes without losing stock."""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig


class Repo:
    def __init__(self, frame):
        self.frame = frame

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
        return self.frame.filter(pl.col("datetime").dt.date().is_in(dates))


def minutes(day=date(2024, 1, 3), start=600, volume=1.):
    return pl.DataFrame([
        {"symbol": "600001.SH",
         "datetime": datetime.combine(day, datetime.min.time()) + timedelta(minutes=start + i),
         "open": 9.8, "high": 9.9, "low": 9.7,
         "close": 10.1 if i == 0 else 9.8, "volume": volume, "amount": 980.}
        for i in range(8)
    ])


def panel():
    return pl.DataFrame([
        {"date": date(2024, 1, 2) + timedelta(days=i), "symbol": "600001.SH",
         "name": "A", "open": 10., "high": 10.2, "low": 9.7, "close": 10.,
         "ma5": 10., "volume": 1000., "score": 1.,
         "signal_ma5_breakdown": i == 1,
         "signal_limit_up": False, "signal_limit_down": False}
        for i in range(3)
    ])


@pytest.mark.parametrize("mode", ["portfolio", "independent"])
def test_signal_sell_starts_next_minute_and_expires_without_next_day_retry(mode):
    frame = minutes(volume=.1)  # ten shares/minute cannot fill a regular board lot
    config = MatcherConfig(
        entry_fill="close_t", exit_fill="signal_next_minute", minute_fill=True,
        initial_capital=10000, max_positions=1, fees_pct=0, slippage_bps=0,
    )
    engine = BacktestEngine(repo=Repo(frame))
    run = engine.simulate_portfolio if mode == "portfolio" else engine.simulate_independent_candidates
    result = run(panel(), pl.Series([True, False, False]),
                 pl.Series([False, True, False]), config,
                 exit_signal_ids=["signal_ma5_breakdown"])
    assert result.stats["execution"]["exit_cancelled_shares"] > 0
    assert result.stats["pending_exit_positions"] == 0
    assert not result.trades or result.trades[0].exit_reason == "end"
    assert not [f for f in result.fills if f["side"] == "sell"]


def test_partial_sell_keeps_remaining_stock_and_records_one_complete_round_trip():
    engine = BacktestEngine(repo=Repo(minutes()))
    config = MatcherConfig(
        entry_fill="close_t", exit_fill="signal_next_minute", minute_fill=True,
        initial_capital=10000, max_positions=1, fees_pct=0, slippage_bps=0,
    )
    result = engine.simulate_portfolio(
        panel(), pl.Series([True, False, False]), pl.Series([False, True, False]), config,
        exit_signal_ids=["signal_ma5_breakdown"])
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert [f["bar"] for f in sells] == [602, 603, 604, 605, 606]
    assert sum(f["shares"] for f in sells) == 500
    assert result.stats["execution"]["exit_cancelled_shares"] == 500
    assert result.equity_curve[1]["cash"] == pytest.approx(4900.)
    assert result.equity_curve[1]["value"] == pytest.approx(9900.)
    assert len(result.trades) == 1
    assert result.trades[0].shares == 1000
    assert result.trades[0].entry_value == 10000
    assert result.trades[0].exit_value == 9900
    assert result.trades[0].pnl_amount == -100


def test_missing_next_minute_does_not_shift_or_extend_order():
    from app.backtest.exit_execution import execute_exit_window

    frame = minutes().filter(pl.col("datetime").dt.minute() != 2)
    order = execute_exit_window(
        frame, symbol="600001.SH", reference_price=10., quantity=1000,
        sell_value=lambda n: n, limit_down_price=9., slippage_bps=0)
    assert [f.bar for f in order.fills] == [603, 604, 605, 606]
    assert order.cancelled_shares == 600


def test_lunch_and_close_window_count_trading_minutes_and_cancel_at_close():
    from app.backtest.exit_execution import execute_exit_window

    frame = pl.concat([minutes(start=689).head(2), minutes(start=781).head(5)])
    order = execute_exit_window(
        frame, symbol="600001.SH", reference_price=10., quantity=1000,
        sell_value=lambda n: n, limit_down_price=9., slippage_bps=0)
    assert [f.bar for f in order.fills] == [781, 782, 783, 784, 785]
    frame = minutes(start=898).head(3)
    order = execute_exit_window(
        frame, symbol="600001.SH", reference_price=10., quantity=1000,
        sell_value=lambda n: n, limit_down_price=9., slippage_bps=0)
    assert [f.bar for f in order.fills] == [900]
    assert order.cancelled_shares == 900


@pytest.mark.parametrize("mode", ["portfolio", "independent"])
def test_partial_at_end_stays_unclosed_in_both_matchers(mode):
    config = MatcherConfig(
        entry_fill="close_t", exit_fill="signal_next_minute", minute_fill=True,
        initial_capital=10000, max_positions=1, fees_pct=0, slippage_bps=0)
    symbol = "688001.SH"
    engine = BacktestEngine(repo=Repo(minutes(volume=.1).with_columns(pl.lit(symbol).alias("symbol"))))
    run = engine.simulate_portfolio if mode == "portfolio" else engine.simulate_independent_candidates
    result = run(panel().head(2).with_columns(pl.lit(symbol).alias("symbol")), pl.Series([True, False]),
                 pl.Series([False, True]), config,
                 exit_signal_ids=["signal_ma5_breakdown"])
    assert result.trades == []
    assert result.stats["pending_exit_positions"] == 0
    assert result.stats["execution"]["exit_cancelled_shares"] > 0
    if mode == "portfolio":
        assert result.equity_curve[-1]["positions"] == 1
        assert result.equity_curve[-1]["value"] == 9990
    else:
        assert result.stats["unclosed_candidates"] == 1


def test_full_order_stops_early_and_aggregate_minimum_commission_is_once():
    from app.backtest.exit_execution import execute_exit_window

    order = execute_exit_window(
        minutes(volume=2.), symbol="600001.SH", reference_price=10., quantity=300,
        sell_value=lambda n: n - max(5, n * .0002) - n * .0005,
        limit_down_price=9.)
    assert [f.bar for f in order.fills] == [602, 603]
    assert [f.shares for f in order.fills] == [200, 100]
    assert order.cancelled_shares == 0
    assert order.fee == pytest.approx(5 + 2940 * .0005)
    assert sum(f.fee for f in order.fills) == pytest.approx(order.fee)


def test_locked_limit_down_is_skipped_and_release_within_window_can_fill():
    from app.backtest.exit_execution import execute_exit_window

    frame = minutes(volume=10.).with_columns([
        pl.when(pl.col("datetime").dt.minute().is_between(2, 3))
        .then(9.).otherwise(pl.col(k)).alias(k) for k in ("open", "high", "low", "close")])
    order = execute_exit_window(
        frame, symbol="600001.SH", reference_price=10., quantity=100,
        sell_value=lambda n: n, limit_down_price=9.)
    assert [f.bar for f in order.fills] == [604]


@pytest.mark.parametrize("mode", ["portfolio", "independent"])
def test_full_window_closes_once_and_preserves_original_quantity(mode):
    config = MatcherConfig(
        entry_fill="close_t", exit_fill="signal_next_minute", minute_fill=True,
        initial_capital=10000, max_positions=1, fees_pct=0, slippage_bps=0)
    engine = BacktestEngine(repo=Repo(minutes(volume=100.)))
    run = engine.simulate_portfolio if mode == "portfolio" else engine.simulate_independent_candidates
    result = run(panel(), pl.Series([True, False, False]),
                 pl.Series([False, True, False]), config,
                 exit_signal_ids=["signal_ma5_breakdown"])
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "signal"
    assert trade.entry_value > 0
    assert trade.shares == trade.raw_entry_shares
    assert trade.exit_price == 9.8
    assert trade.exit_value == pytest.approx(trade.shares * 9.8)
    assert len(result.exit_orders) == 1
