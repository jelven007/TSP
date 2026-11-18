"""The March-start S01 replay builds its own frozen previous-session selection."""
from datetime import date, datetime

import polars as pl
from test_s01_optional_triggers import optional
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import _service

from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig
from app.strategy import custom_signals


def test_march_30_replay_uses_frozen_history_without_loading_live_selections(tmp_path):
    service = _service(tmp_path)
    mapping = {
        date(2026, 8, 21): date(2026, 3, 26),
        date(2026, 8, 24): date(2026, 3, 27),
        date(2026, 8, 25): date(2026, 3, 30),
        date(2026, 8, 26): date(2026, 3, 31),
    }
    load = service.engine.load_panel_for_backtest
    panel = load().with_columns(pl.col("date").replace_strict(mapping).alias("date"))
    service.engine.load_panel_for_backtest = lambda *args, **kwargs: panel
    repo = service.engine.repo
    read_minute = repo.get_minute_by_dates
    inverse = {new: old for old, new in mapping.items()}

    def minutes(symbols, days, asset_type):
        frame = read_minute(symbols, [inverse[days[0]]], asset_type)
        offset = datetime.combine(days[0], datetime.min.time()) - datetime.combine(
            inverse[days[0]], datetime.min.time())
        return frame.with_columns((pl.col("datetime") + offset).alias("datetime"))

    repo.get_minute_by_dates = minutes
    repo.list_minute_dates = lambda *args: [date(2026, 3, 30), date(2026, 3, 31)]
    strategy = optional(service.strategy_engine)
    strategy.meta["result_sections"] = "signals"
    selection_reads = []
    service.strategy_engine._selection_loader = lambda *args: selection_reads.append(args)
    entry = {**ENTRY, "params": {**ENTRY["params"], "time": "14:45:00",
                                "evaluation_delay_minutes": 1, "current_return_min": 0}}
    with custom_signals.private_signal_registry([entry, EXIT]):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 3, 30), date(2026, 3, 31),
            initial_capital=1000000, params={"initial_cash": 1000000}),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert any(fill["side"] == "buy" and fill["datetime"].startswith("2026-03-30") for fill in result.fills)
    assert selection_reads == []
