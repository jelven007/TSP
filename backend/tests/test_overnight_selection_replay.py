"""Historical S01 replay must supply eligibility and reject invalid entry evidence."""
from datetime import date

import polars as pl
import pytest
from test_s01_optional_triggers import optional
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import _service

from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig
from app.strategy import custom_signals


@pytest.mark.parametrize("sections", [False, True])
@pytest.mark.parametrize("start,delay,expected", [
    ("14:45:00", 1, [887, 888, 889, 890, 891]),
    ("14:44:20", .5, [885, 886, 887, 888, 889]),
])
def test_historical_selection_preserves_confirmation_window(tmp_path, sections, start, delay, expected):
    service = _service(tmp_path)
    original = service.engine.repo.get_minute_by_dates
    service.engine.repo.get_minute_by_dates = lambda *args, **kwargs: original(
        *args, **kwargs).with_columns(pl.when(pl.col("datetime").dt.hour() == 14)
                                     .then(1.).otherwise(pl.col("volume")).alias("volume"))
    strategy = optional(service.strategy_engine)
    if sections:
        strategy.meta["result_sections"] = "signals"
    signal = {**ENTRY, "params": {**ENTRY["params"], "time": start,
                                 "evaluation_delay_minutes": delay, "current_return_min": 0}}
    with custom_signals.private_signal_registry([signal, EXIT]):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26),
            params={"initial_cash": 1000000, "participation": .001}),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert sorted({f["bar"] for f in result.fills if f["side"] == "buy"}) == expected
    assert result.config["parameter_signals"]["entry"] == signal


def test_invalid_entry_cannot_be_a_successful_zero_trade_backtest(tmp_path):
    service = _service(tmp_path)
    optional(service.strategy_engine)
    run = service.strategy_engine.run

    def invalid_entry(*args, **kwargs):
        result = run(*args, **kwargs)
        result.rows, result.entry_signal_hits = [], []
        result.entry_filter_error = "入场参考证据无效"
        return result

    service.strategy_engine.run = invalid_entry
    with custom_signals.private_signal_registry([ENTRY, EXIT]):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26),
            params={"initial_cash": 1000000}),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error and "2026-08-25" in result.error and "入场参考证据无效" in result.error
    assert result.stats["status"] == "rejected_before_NAV"
    assert not result.equity_curve
