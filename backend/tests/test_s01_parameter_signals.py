"""S01 parameter signals must control the original selection and exit paths."""
from copy import deepcopy
from datetime import date, datetime

import pytest
from test_s01_strategy import BUILTINS, _service, bars, daily

from app.strategy import custom_signals
from app.strategy.engine import StrategyDataContext, StrategyEngine

ENTRY = {
    "id": "late_session_strength_s01", "name": "尾盘建仓_s01", "kind": "entry",
    "template": "s01_late_entry", "timeframe": "intraday", "enabled": True, "conditions": [],
    "params": {"current_amount_min": 1e8, "current_return_min": .02,
               "current_return_max": .03, "current_range_max": .0525,
               "market_min_valid": 1, "market_positive_min": .65},
}
EXIT = {
    "id": "morning_liquidation_s01", "name": "早盘清仓_s01", "kind": "exit",
    "template": "s01_morning_exit", "timeframe": "intraday", "enabled": True, "conditions": [],
    "params": {"vwap_buffer": .0003, "peak_profit_min": .02, "peak_retrace_min": .005,
               "return_15m_max": -.003, "decline_3m": .002, "volume_ratio_min": 1.5,
               "guard_time": "11:26", "sell_deadline": "13:30"},
}


def bind(engine):
    strategy = engine.get("s01")
    strategy.meta = deepcopy(strategy.meta)
    strategy.meta["parameter_signals"] = {"entry": ENTRY["id"], "exit": EXIT["id"]}
    keys = ENTRY["params"].keys() | EXIT["params"].keys()
    strategy.meta["params"] = [p for p in strategy.meta["params"] if p["id"] not in keys]
    return strategy


@pytest.mark.parametrize("signal", [ENTRY, EXIT])
def test_s01_templates_validate_and_skip_generic_column_compiler(signal):
    custom_signals.validate(signal)
    assert custom_signals.build_intraday_expressions([signal]) == {}


@pytest.mark.parametrize("patch", [
    {"params": {**ENTRY["params"], "market_min_valid": True}},
    {"params": {**ENTRY["params"], "current_return_max": .01}},
    {"params": {**ENTRY["params"], "current_amount_min": float("nan")}},
    {"params": {**ENTRY["params"], "time": "14:30:00"}},
    {"kind": "exit"}, {"conditions": [{"left": "close", "op": ">", "right": "1"}]},
])
def test_invalid_s01_entry_definitions_rejected(patch):
    with pytest.raises(ValueError):
        custom_signals.validate({**ENTRY, **patch})


@pytest.mark.parametrize("patch", [
    {"guard_time": "11:30"}, {"sell_deadline": "12:30"},
    {"sell_deadline": "11:26"}, {"vwap_buffer": -.01}, {"volume_ratio_min": False},
])
def test_invalid_s01_exit_values_rejected(patch):
    with pytest.raises(ValueError):
        custom_signals.validate({**EXIT, "params": {**EXIT["params"], **patch}})


def test_saved_signal_changes_actual_entry_even_with_stale_strategy_params():
    engine = StrategyEngine([BUILTINS])
    bind(engine)
    context = StrategyDataContext(
        "stock", "1m", date(2026, 8, 25), current=daily().tail(1),
        history=bars(price=10.25), daily_history=daily())
    with custom_signals.private_signal_registry([ENTRY, EXIT]):
        assert engine.run("s01", context, params={"current_return_max": 0}).total == 1
    tightened = {**ENTRY, "params": {**ENTRY["params"], "current_return_max": .021}}
    with custom_signals.private_signal_registry([tightened, EXIT]):
        assert engine.run("s01", context, params={"current_return_max": 1}).total == 0


def test_saved_exit_guard_controls_actual_holding_exit():
    engine = StrategyEngine([BUILTINS])
    bind(engine)
    context = StrategyDataContext(
        "stock", "1m", date(2026, 8, 25), current=daily().tail(1), daily_history=daily(),
        history=bars(end=690, price=11.),
        positions=[{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                    "entry_unit_cost": 10., "sell_date": "2026-08-25"}],
        completed_through=datetime(2026, 8, 25, 11, 25))
    with custom_signals.private_signal_registry([ENTRY, EXIT]):
        assert not engine.run("s01", context).exit_signal_hits
        context.completed_through = datetime(2026, 8, 25, 11, 26)
        hit = engine.run("s01", context).exit_signal_hits[0]
        assert hit["signal_bar"] == 686
        assert hit["last_eligible_bar"] == 810


@pytest.mark.parametrize("definitions", [[], [ENTRY], [ENTRY, {**EXIT, "enabled": False}],
                                        [ENTRY, {**EXIT, "template": "morning_liquidation"}]])
def test_broken_binding_fails_instead_of_using_old_defaults(definitions):
    engine = StrategyEngine([BUILTINS])
    bind(engine)
    with custom_signals.private_signal_registry(definitions), pytest.raises(ValueError, match="S01"):
        engine.run("s01", StrategyDataContext(
            "stock", "1m", date(2026, 8, 25), current=daily().tail(1),
            history=bars(), daily_history=daily()))


def test_backtest_freezes_bound_values_and_definitions(tmp_path):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    bind(service.strategy_engine)
    entry = {**ENTRY, "params": {**ENTRY["params"], "current_return_min": 0}}
    with custom_signals.private_signal_registry([entry, EXIT]):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26),
            params={"initial_cash": 1000000, "guard_time": "11:22"}),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert result.config["params"]["guard_time"] == "11:26"
    assert result.config["parameter_signals"]["exit"] == EXIT
    assert result.signals[0]["signal_bar"] == 686


def test_detail_exposes_signal_values_without_duplicate_strategy_parameters():
    from app.api.strategy import _strategy_detail

    engine = StrategyEngine([BUILTINS])
    strategy = bind(engine)
    with custom_signals.private_signal_registry([ENTRY, EXIT]):
        detail = _strategy_detail(strategy, {"params": {"guard_time": "11:22"}}, engine)
    assert detail["parameter_signals"] == {"entry": ENTRY, "exit": EXIT}
    assert "guard_time" not in detail["params_defaults"]


def test_detail_reads_account_document_storage_without_custom_directory(tmp_path):
    from app.api.strategy import _strategy_detail

    engine = StrategyEngine([BUILTINS])
    strategy = bind(engine)
    engine.documents = tmp_path
    assert engine._user_data_dir() is None
    custom_signals.save_one(tmp_path, ENTRY)
    custom_signals.save_one(tmp_path, EXIT)
    detail = _strategy_detail(strategy, engine=engine)
    assert detail["parameter_signals"] == {"entry": ENTRY, "exit": EXIT}
