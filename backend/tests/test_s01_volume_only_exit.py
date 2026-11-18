"""Current S01 exits use only recent volume decline, plus unconditional clocks."""
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest
from test_s01_exit_schedule import NEW_EXIT, SCHEDULE, scheduled_replay
from test_s01_optional_triggers import context, optional
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import BUILTINS, bars

from app.backtest.overnight import order_fee
from app.strategy import custom_signals
from app.strategy.builtin._overnight import exit_signal, parameters
from app.strategy.engine import StrategyEngine
from app.strategy.s01_signals import resolve_parameters, signal_parameters

VOLUME_PARAMS = {
    "decline_3m": .002, "volume_ratio_min": 1.5,
    "start_time": "11:15:00", "stop_time": "11:26:00", "fallback_time": "13:30:00",
}
VOLUME_EXIT = {**EXIT, "params": VOLUME_PARAMS}
REMOVED = {"vwap_buffer", "peak_profit_min", "peak_retrace_min", "return_15m_max"}


def test_volume_only_signal_accepts_exactly_two_thresholds_and_three_clocks():
    assert signal_parameters(VOLUME_EXIT) == VOLUME_PARAMS
    custom_signals.validate(VOLUME_EXIT)
    assert signal_parameters(EXIT) == EXIT["params"]
    assert signal_parameters(NEW_EXIT) == SCHEDULE


@pytest.mark.parametrize("patch", [
    {"decline_3m": None}, {"decline_3m": -.001}, {"decline_3m": True},
    {"volume_ratio_min": float("nan")}, {"vwap_buffer": .0003},
    {"guard_time": "11:26"}, {"stop_time": "11:26:01"},
])
def test_volume_only_signal_rejects_invalid_or_partially_restored_values(patch):
    with pytest.raises(ValueError):
        signal_parameters({**VOLUME_EXIT, "params": {**VOLUME_PARAMS, **patch}})


def test_new_values_do_not_reacquire_obsolete_conditions_from_shared_defaults():
    normalized = parameters(parameters(VOLUME_PARAMS))
    assert REMOVED.isdisjoint(normalized)
    assert normalized["decline_3m"] == .002
    assert normalized["volume_ratio_min"] == 1.5


@pytest.mark.parametrize("current_baseline", [False, True])
def test_selected_volume_only_values_prune_baseline_and_stale_strategy_overrides(current_baseline):
    engine = StrategyEngine([BUILTINS])
    strategy = optional(engine)
    if current_baseline:
        strategy.meta["trigger_parameter_defaults"] = {**ENTRY["params"], **VOLUME_PARAMS}
    resolved = resolve_parameters(engine, strategy, {**SCHEDULE, "participation": .07},
                                  definitions={"exit": VOLUME_EXIT})
    assert REMOVED.isdisjoint(resolved)
    assert resolved["decline_3m"] == .002
    assert resolved["participation"] == .07
    assert resolved["guard_time"] == "11:26"
    assert resolved["sell_deadline"] == "13:35"


@pytest.mark.parametrize("condition", ["vwap", "trailing"])
def test_removed_morning_conditions_do_not_sell_under_new_definition(condition):
    price, close = (10., 9.96) if condition == "vwap" else (10.3, 10.24)
    frame = bars(end=690, price=price).with_columns(
        pl.when(pl.col("datetime") == datetime(2026, 8, 25, 11, 15))
        .then(close).otherwise(pl.col("close")).alias("close"))
    old = {**SCHEDULE, **({"vwap_buffer": 1.} if condition == "trailing" else {})}
    assert exit_signal(frame, 675, 10., old)["reason"] == "morning_vwap_or_trailing"
    for bar in range(675, 686):
        assert exit_signal(frame, bar, 10., VOLUME_PARAMS) is None


def volume_frame(*, close=9.975, volume=1500.):
    return bars(end=675, price=10.).filter(
        pl.col("datetime") >= datetime(2026, 8, 25, 11, 3)).with_columns(
        pl.when(pl.col("datetime") >= datetime(2026, 8, 25, 11, 13))
        .then(close).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("datetime") >= datetime(2026, 8, 25, 11, 13))
        .then(volume).otherwise(pl.col("volume")).alias("volume"),
    ).select("symbol", "datetime", "close", "volume")


def test_recent_volume_decline_suffices_without_cost_vwap_or_fifteen_minute_history():
    hit = exit_signal(volume_frame(), 675, None, VOLUME_PARAMS)
    assert hit["reason"] == "volume_decline"
    assert hit["ret_3m"] == pytest.approx(-.0025)
    assert hit["volume_ratio_3m"] == 1.5
    assert hit["exit_stage"] == "conditional"
    assert (hit["first_eligible_bar"], hit["last_eligible_bar"]) == (676, 680)
    assert exit_signal(volume_frame(), 674, None, VOLUME_PARAMS) is None


@pytest.mark.parametrize("close,volume", [(9.99, 1500.), (9.975, 1490.)])
def test_both_decline_and_volume_thresholds_are_required(close, volume):
    assert exit_signal(volume_frame(close=close, volume=volume), 675, None, VOLUME_PARAMS) is None


def test_stop_and_fallback_rearm_new_volume_only_exit_without_market_evidence():
    pending = exit_signal(volume_frame(), 675, None, VOLUME_PARAMS)
    stop = exit_signal(pl.DataFrame(), 686, None, VOLUME_PARAMS, pending)
    fallback = exit_signal(pl.DataFrame(), 810, None, VOLUME_PARAMS, stop)
    assert (stop["reason"], stop["signal_bar"], stop["last_eligible_bar"]) == ("time_stop", 686, 781)
    assert (fallback["reason"], fallback["signal_bar"], fallback["last_eligible_bar"]) == ("time_fallback", 810, 815)


def test_saved_custom_source_runs_new_volume_exit_and_keeps_frozen_signal_independent(tmp_path):
    source = Path(__file__).parent / "fixtures" / "s01_available_minutes.py"
    (tmp_path / "s01.py").write_text(source.read_text())
    engine = StrategyEngine([tmp_path])
    assert engine.get("custom_mvxxcs01").meta["name"] == "s01"
    ctx = context(end=815, price=11.)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": None, "entry_date": "2026-08-24", "sell_date": "2026-08-25"}]
    frozen = {**EXIT, "id": "frozen", "params": {**EXIT["params"], "guard_time": "11:22"}}
    with custom_signals.private_signal_registry([VOLUME_EXIT, frozen]):
        result = engine.run("custom_mvxxcs01", ctx, overrides={
            "entry_signals": [], "exit_signals": ["csgi_" + EXIT["id"], "csgi_frozen"]})
    assert [(h["signal_bar"], h["signals"]) for h in result.exit_signal_hits] == [
        (682, ["csgi_frozen"]), (686, ["csgi_" + EXIT["id"]]), (810, ["csgi_" + EXIT["id"]])]


def test_new_definition_ignores_old_price_trigger_then_sells_remainder_at_both_clocks(tmp_path):
    result = scheduled_replay(tmp_path, conditional=True, exit_definition=VOLUME_EXIT)
    assert result.error is None
    assert [h["signal_bar"] for h in result.signals] == [686, 810]
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert sells[0]["bar"] == 687
    assert any(f["bar"] == 811 for f in sells)
    assert not result.stats["execution"]["remaining_positions"]
    assert sum(f["fee"] for f in sells) == pytest.approx(
        order_fee(sum(f["notional"] for f in sells), "sell", result.config["params"]))
    assert REMOVED.isdisjoint(result.config["params"])
    assert result.config["trigger_definitions"]["exit"][0]["params"] == VOLUME_PARAMS
