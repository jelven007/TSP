"""Frozen signal references must govern all daily execution paths."""
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig

METHODS = [
    "simulate_portfolio", "simulate_portfolio_legacy",
    "simulate_independent_candidates", "simulate_independent_candidates_legacy",
]


def run(method, policy, *, opening=10., atr=.2, scale=1., **config):
    frame = pl.DataFrame({
        "symbol": ["600001.SH"] * 4,
        "date": [date(2024, 1, 2) + timedelta(days=i) for i in range(4)],
        "open": [10., opening, 10., 10.],
        "close": [10.] * 4, "raw_close": [10. / scale] * 4,
        "high": [10.2, max(opening, 10.2), 10.2, 10.2],
        "low": [9.7, 9.6, 9.3, 9.3], "volume": [10000.] * 4,
        "atr_14": [atr, 8., 8., 8.],
    })
    return getattr(BacktestEngine(None), method)(
        frame, pl.Series([True, False, False, False]), pl.Series([False] * 4),
        MatcherConfig(
            matching="open_t+1", fees_pct=0, slippage_bps=0, initial_capital=100_000,
            max_positions=1, entry_risk=policy, **config,
        ),
    )


@pytest.mark.parametrize("method", METHODS)
def test_high_open_is_rejected_at_execution(method):
    result = run(method, {"max_entry_premium_pct": .03}, opening=10.5)
    assert result.trades == []
    assert result.stats["execution"]["buy_entry_premium"] == 1


@pytest.mark.parametrize("method", METHODS)
def test_atr_is_frozen_at_signal_and_never_widens(method):
    result = run(method, {"stop_atr_multiple": 2.})
    assert result.trades[0].exit_price == pytest.approx(9.6)
    assert result.trades[0].exit_reason == "frozen_stop"


@pytest.mark.parametrize("method", METHODS)
def test_signal_low_is_not_replaced_with_entry_day_low(method):
    result = run(method, {"stop_signal_low": True})
    assert result.trades[0].exit_price == pytest.approx(9.7)


@pytest.mark.parametrize("method", METHODS)
def test_missing_required_atr_fails_closed(method):
    result = run(method, {"stop_atr_multiple": 2.}, atr=None)
    assert result.trades == []
    assert result.stats["execution"]["buy_missing_risk_reference"] == 1


@pytest.mark.parametrize("method", METHODS[:2])
@pytest.mark.parametrize("scale", [1., .5])
def test_risk_budget_caps_actual_raw_quantity(method, scale):
    result = run(method, {"stop_atr_multiple": 2., "risk_per_trade_pct": .005}, scale=scale)
    trade = result.trades[0]
    # 500 currency risk / .4 adjusted distance, rounded down to raw 100-share lots.
    assert trade.raw_entry_shares == (1200 if scale == 1 else 600)
    assert abs(trade.pnl_amount) <= 500


def test_existing_fixed_stop_remains_a_ceiling_on_loss():
    result = run(METHODS[0], {"stop_atr_multiple": 5.}, stop_loss_pct=.03)
    assert result.trades[0].exit_price == pytest.approx(9.7)
    assert result.trades[0].exit_reason == "stop_loss"


@pytest.mark.parametrize("values", [
    {"risk_per_trade_pct": True}, {"risk_per_trade_pct": .2},
    {"max_entry_premium_pct": -.01}, {"stop_atr_multiple": float("nan")},
    {"stop_signal_low": 1}, {"unknown": 1},
])
def test_invalid_entry_risk_is_rejected(values):
    with pytest.raises(ValueError):
        MatcherConfig(entry_risk=values)


def test_entry_risk_dependencies_and_api_validation():
    from fastapi import HTTPException

    from app.api.strategy import _validate_entry_risk
    from app.backtest.strategy import StrategyDependencyResolver
    from app.strategy.engine import StrategyEngine

    strategy = StrategyEngine._load_file(
        Path(__file__).resolve().parents[2] / "app/strategy/builtin/strong_open.py",
    )
    overrides = {"entry_risk": {"stop_atr_multiple": 2.}}
    plan = StrategyDependencyResolver().resolve(
        strategy, params={}, basic_filter={}, entry_signals=[], exit_signals=[],
        overrides=overrides,
    )
    assert "atr_14" in plan.matrix_columns
    assert "atr_14" in StrategyEngine._matrix_field_columns(strategy, overrides)
    _validate_entry_risk(overrides, strategy)
    with pytest.raises(HTTPException) as caught:
        _validate_entry_risk({"stop_loss": None, "entry_risk": {"risk_per_trade_pct": .005}}, strategy)
    assert caught.value.status_code == 400


@pytest.mark.parametrize("method", METHODS[:2])
def test_high_rank_rejection_does_not_consume_slot_for_valid_candidate(method):
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(4)]
    frame = pl.DataFrame([
        {
            "symbol": symbol, "date": day, "close": 10., "raw_close": 10.,
            "open": 10.5 if symbol == "600001.SH" and i == 1 else 10.,
            "high": 10.5, "low": 9.5, "volume": 10000.,
            "score": 100. if symbol == "600001.SH" else 50.,
        }
        for symbol in ("600001.SH", "600002.SH") for i, day in enumerate(days)
    ])
    result = getattr(BacktestEngine(None), method)(
        frame, frame["date"] == days[0], pl.Series([False] * 8),
        MatcherConfig(matching="open_t+1", max_positions=1, entry_risk={"max_entry_premium_pct": .03}),
    )
    assert [t.symbol for t in result.trades] == ["600002.SH"]
    assert result.stats["execution"]["buy_entry_premium"] == 1


def test_frozen_stop_is_serialized_in_raw_entry_units():
    from app.backtest.strategy import StrategyBacktestService

    result = run(METHODS[0], {"stop_atr_multiple": 2.}, scale=.5)
    record = result.trades[0]
    assert record.raw_frozen_stop_price == pytest.approx(19.2)
    assert StrategyBacktestService._trade_to_dict(record)["raw_frozen_stop_price"] == pytest.approx(19.2)
