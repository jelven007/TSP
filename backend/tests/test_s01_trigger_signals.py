"""S01 uses the ordinary entry/exit references without changing its evaluator."""
from copy import deepcopy
from datetime import date, datetime

import pytest
from fastapi import HTTPException
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import _service, bars, daily, s01_engine

from app.api.strategy import _strategy_detail, _validate_overlay_config
from app.strategy import custom_signals
from app.strategy.engine import StrategyDataContext

REFS = {"entry_signals": ["csgi_" + ENTRY["id"]], "exit_signals": ["csgi_" + EXIT["id"]]}


def with_triggers(engine):
    strategy = engine.get("s01")
    strategy.meta = deepcopy(strategy.meta)
    strategy.meta.pop("parameter_signals", None)
    keys = ENTRY["params"].keys() | EXIT["params"].keys()
    strategy.meta["params"] = [p for p in strategy.meta["params"] if p["id"] not in keys]
    strategy.entry_signals = REFS["entry_signals"].copy()
    strategy.exit_signals = REFS["exit_signals"].copy()
    return strategy


def test_ordinary_trigger_references_drive_actual_entry_and_holding_exit():
    engine = s01_engine()
    with_triggers(engine)
    context = StrategyDataContext(
        "stock", "1m", date(2026, 8, 25), current=daily().tail(1),
        history=bars(price=10.25), daily_history=daily())
    with custom_signals.private_signal_registry([ENTRY, EXIT]):
        result = engine.run("s01", context, params={"current_return_max": 0})
        assert result.total == 1
        assert result.entry_signal_hits[0]["signals"] == REFS["entry_signals"]
        context.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                              "entry_unit_cost": 10., "sell_date": "2026-08-25"}]
        context.completed_through = datetime(2026, 8, 25, 11, 26)
        context.history = bars(end=690, price=11.)
        hit = engine.run("s01", context).exit_signal_hits[0]
        assert hit["signals"] == REFS["exit_signals"]
        assert (hit["signal_bar"], hit["last_eligible_bar"]) == (686, 810)


def test_selected_override_is_the_only_parameter_source_in_execution_and_detail():
    engine = s01_engine()
    strategy = with_triggers(engine)
    alternate = {**ENTRY, "id": "tighter_s01",
                 "params": {**ENTRY["params"], "current_return_max": .021}}
    overrides = {**REFS, "entry_signals": ["csgi_tighter_s01"]}
    with custom_signals.private_signal_registry([ENTRY, EXIT, alternate]):
        detail = _strategy_detail(strategy, overrides, engine)
        result = engine.run("s01", StrategyDataContext(
            "stock", "1m", date(2026, 8, 25), current=daily().tail(1),
            history=bars(price=10.25), daily_history=daily()),
            params={"current_return_max": 1}, overrides=overrides)
    assert detail["parameter_signals"]["entry"] == alternate
    assert detail["entry_signals"] == ["csgi_tighter_s01"]
    assert result.total == 0


def test_config_save_accepts_correct_s01_trigger_selection():
    engine = s01_engine()
    strategy = with_triggers(engine)
    with custom_signals.private_signal_registry([ENTRY, EXIT]):
        _validate_overlay_config(REFS, strategy, engine)


@pytest.mark.parametrize("patch", [
    {"entry_signals": []}, {"exit_signals": []},
    {"entry_signals": ["csgi_missing"]},
    {"entry_signals": REFS["entry_signals"] * 2},
    {"exit_signals": REFS["entry_signals"]},
    {"entry_signals": ["signal_ma_golden_cross"]},
    {"entry_signals": [None]}, {"entry_signals": "csgi_missing"},
])
def test_save_and_execution_reject_incomplete_or_incompatible_trigger_selection(patch):
    engine = s01_engine()
    strategy = with_triggers(engine)
    overrides = {**REFS, **patch}
    with custom_signals.private_signal_registry([ENTRY, EXIT]):
        with pytest.raises(HTTPException) as error:
            _validate_overlay_config(overrides, strategy, engine)
        assert error.value.status_code == 400
        with pytest.raises(ValueError, match="S01"):
            engine.run("s01", StrategyDataContext(
                "stock", "1m", date(2026, 8, 25), current=daily().tail(1),
                history=bars(), daily_history=daily()), overrides=overrides)


def test_backtest_accepts_and_freezes_ordinary_s01_trigger_references(tmp_path):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    with_triggers(service.strategy_engine)
    entry = {**ENTRY, "params": {**ENTRY["params"], "current_return_min": 0}}
    with custom_signals.private_signal_registry([entry, EXIT]):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26),
            overrides=REFS, params={"initial_cash": 1000000, "guard_time": "11:22"}),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert result.config["entry_signals"] == REFS["entry_signals"]
    assert result.config["exit_signals"] == REFS["exit_signals"]
    assert result.config["params"]["guard_time"] == "11:26"
    assert result.config["parameter_signals"] == {"entry": entry, "exit": EXIT}
    assert result.signals[0]["signal_bar"] == 686


def test_s01_templates_cannot_be_selected_for_generic_minute_strategies():
    engine = s01_engine()
    strategy = with_triggers(engine)
    strategy.meta.pop("minute_execution")
    with custom_signals.private_signal_registry([ENTRY, EXIT]), pytest.raises(HTTPException) as error:
        _validate_overlay_config(REFS, strategy, engine)
    assert error.value.status_code == 400
