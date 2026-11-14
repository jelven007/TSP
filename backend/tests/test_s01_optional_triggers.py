"""Optional S01 selections must change signals and actual orders, including empty sets."""
from copy import deepcopy
from datetime import date, datetime

import polars as pl
import pytest
from fastapi import HTTPException
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import BUILTINS, _service, bars, daily

from app.api.strategy import _strategy_detail, _validate_overlay_config
from app.strategy import custom_signals
from app.strategy.engine import StrategyDataContext, StrategyEngine

MINUTE_ENTRY = {
    "id": "other_entry", "name": "另一个买点", "kind": "entry", "enabled": True,
    "timeframe": "intraday", "conditions": [{"left": "vol_ratio_3m_today", "op": ">", "right": "0.5"}],
}
MINUTE_EXIT = {
    "id": "other_exit", "name": "另一个卖点", "kind": "exit", "enabled": True,
    "timeframe": "intraday", "conditions": [{"left": "vol_ratio_3m_today", "op": ">", "right": "0.5"}],
}
DAILY_ENTRY = {
    "id": "daily_entry", "name": "前日买点", "kind": "entry", "enabled": True,
    "conditions": [{"left": "close", "op": ">", "right": "9"}],
}
ALL = [ENTRY, EXIT, MINUTE_ENTRY, MINUTE_EXIT, DAILY_ENTRY]


def optional(engine):
    s = engine.get("s01")
    s.meta = deepcopy(s.meta)
    s.meta.pop("parameter_signals", None)
    s.meta["trigger_selection"] = "optional_v1"
    s.meta["trigger_parameter_defaults"] = {**ENTRY["params"], **EXIT["params"]}
    s.entry_signals = ["csgi_" + ENTRY["id"]]
    s.exit_signals = ["csgi_" + EXIT["id"]]
    return s


def context(price=10.25, end=885):
    return StrategyDataContext(
        "stock", "1m", date(2026, 8, 25), current=daily().tail(1),
        history=bars(price=price, end=end), daily_history=daily(),
        completed_through=datetime(2026, 8, 25, end // 60, end % 60),
    )


@pytest.mark.parametrize("selection", [
    {"entry_signals": [], "exit_signals": []},
    {"entry_signals": ["signal_macd_golden"], "exit_signals": ["signal_macd_dead"]},
    {"entry_signals": ["signal_limit_up"], "exit_signals": ["signal_limit_down"]},
    {"entry_signals": ["csg_daily_entry"], "exit_signals": ["csgi_other_exit"]},
    {"entry_signals": ["csgi_other_entry", "csgi_" + ENTRY["id"]], "exit_signals": []},
])
def test_save_accepts_empty_other_and_multiple_signals_and_keeps_entrypoints(selection):
    e = StrategyEngine([BUILTINS])
    s = optional(e)
    with custom_signals.private_signal_registry(ALL):
        _validate_overlay_config(selection, s, e)
        detail = _strategy_detail(s, selection, e)
    assert detail["optional_triggers"] is True
    assert detail["entry_signals"] == selection["entry_signals"]
    assert detail["exit_signals"] == selection["exit_signals"]


@pytest.mark.parametrize("names", [["csgi_missing"], ["csgi_other_exit"], [None],
                                  ["signal_unknown"], ["csgi_other_entry"] * 2, "csgi_other_entry"])
def test_invalid_optional_selection_is_rejected_before_save_or_execution(names):
    e = StrategyEngine([BUILTINS])
    s = optional(e)
    with custom_signals.private_signal_registry(ALL):
        with pytest.raises(HTTPException) as error:
            _validate_overlay_config({"entry_signals": names}, s, e)
        assert error.value.status_code == 400
        with pytest.raises(ValueError):
            e.run("s01", context(), overrides={"entry_signals": names})


def test_clearing_entry_removes_native_signal_thresholds_and_clearing_exit_removes_guard():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(price=10.5)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10, "sell_date": "2026-08-25"}]
    with custom_signals.private_signal_registry(ALL):
        assert e.run("s01", ctx).total == 0
        result = e.run("s01", ctx, overrides={"entry_signals": [], "exit_signals": []})
    assert result.total == 1
    assert result.entry_signal_hits == [{"symbol": "600001.SH", "signals": []}]
    assert result.exit_signal_hits == []


@pytest.mark.parametrize("other", ["csgi_other_entry", "csg_daily_entry", "signal_macd_golden"])
def test_multiple_entry_signals_use_or_and_report_only_actual_hits(other):
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(price=10.5)
    ctx.daily_history = ctx.daily_history.with_columns(pl.lit(True).alias("signal_macd_golden"))
    with custom_signals.private_signal_registry(ALL):
        result = e.run("s01", ctx, overrides={
            "entry_signals": ["csgi_" + ENTRY["id"], other], "exit_signals": []})
    assert result.total == 1
    assert result.entry_signal_hits == [{"symbol": "600001.SH", "signals": [other]}]


def test_multiple_native_entries_do_not_merge_thresholds_and_generic_false_blocks_buy():
    e = StrategyEngine([BUILTINS])
    optional(e)
    tighter = {**ENTRY, "id": "tight", "params": {**ENTRY["params"], "current_return_max": .021}}
    never = {**MINUTE_ENTRY, "conditions": [{"left": "price", "op": ">", "right": "100"}]}
    with custom_signals.private_signal_registry([ENTRY, EXIT, tighter, never]):
        result = e.run("s01", context(), overrides={"entry_signals": ["csgi_tight", "csgi_" + ENTRY["id"]]})
        assert result.entry_signal_hits == [{"symbol": "600001.SH", "signals": ["csgi_" + ENTRY["id"]]}]
        result = e.run("s01", context(), overrides={"entry_signals": ["csgi_other_entry"]})
        assert result.total == 0


def test_generic_exit_replaces_native_guard_and_first_hit_of_multiple_exits_wins():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=690)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10., "sell_date": "2026-08-25"}]
    with custom_signals.private_signal_registry(ALL):
        result = e.run("s01", ctx, overrides={"exit_signals": ["csgi_other_exit", "csgi_" + EXIT["id"]]})
    hit = result.exit_signal_hits[0]
    assert hit["signals"] == ["csgi_other_exit"]
    # The shared volume feature needs six completed bars before its first edge.
    assert hit["signal_bar"] == 576
    assert hit["first_eligible_bar"] == 577


@pytest.mark.parametrize("selection, buys, sells", [
    ({"entry_signals": [], "exit_signals": []}, True, False),
    ({"entry_signals": ["csgi_other_entry"], "exit_signals": ["csgi_other_exit"]}, True, True),
    ({"entry_signals": ["csg_daily_entry"], "exit_signals": []}, True, False),
])
def test_optional_backtest_orders_use_frozen_selection(tmp_path, selection, buys, sells):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    optional(service.strategy_engine)
    with custom_signals.private_signal_registry(ALL):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26),
            params={"initial_cash": 1000000}, overrides=selection),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert any(f["side"] == "buy" for f in result.fills) == buys
    assert any(f["side"] == "sell" for f in result.fills) == sells
    assert result.config["entry_signals"] == selection["entry_signals"]
    assert result.config["exit_signals"] == selection["exit_signals"]
    if not sells:
        assert result.stats["execution"]["remaining_positions"]
    else:
        assert result.signals[0]["signals"] == ["csgi_other_exit"]
        assert result.signals[0]["signal_bar"] == 576


def test_default_optional_native_selection_retains_original_timing_and_values(tmp_path):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    optional(service.strategy_engine)
    entry = {**ENTRY, "params": {**ENTRY["params"], "current_return_min": 0}}
    with custom_signals.private_signal_registry([entry, EXIT]):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26), params={"initial_cash": 1000000}),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert result.config["params"]["guard_time"] == "11:26"
    assert result.signals[0]["signal_bar"] == 686
    assert result.signals[0]["last_eligible_bar"] == 810


def test_generic_entry_cannot_use_later_minutes_or_current_day_final_daily_values():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=890)
    ctx.history = ctx.history.with_columns(pl.when(pl.col("datetime") > datetime(2026, 8, 25, 14, 45))
                                          .then(21.).otherwise(pl.col("close")).alias("close"))
    later = {**MINUTE_ENTRY, "conditions": [{"left": "price", "op": ">", "right": "20"}]}
    future = {**DAILY_ENTRY, "conditions": [{"left": "close", "op": ">", "right": "20"}]}
    ctx.daily_history = pl.concat([ctx.daily_history, ctx.daily_history.tail(1).with_columns(
        pl.lit(ctx.as_of).alias("date"), pl.lit(100.).alias("close"))])
    with custom_signals.private_signal_registry([ENTRY, EXIT, later, future]):
        for names in (["csgi_other_entry"], ["csg_daily_entry"]):
            result = e.run("s01", ctx, overrides={"entry_signals": names, "exit_signals": []})
            assert result.total == 0


def test_stopped_selected_signal_is_rejected_and_two_native_exits_keep_independent_clocks():
    e = StrategyEngine([BUILTINS])
    optional(e)
    early = {**EXIT, "id": "early", "params": {**EXIT["params"], "guard_time": "11:22"}}
    ctx = context(end=690)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10., "sell_date": "2026-08-24"}]
    with custom_signals.private_signal_registry([ENTRY, EXIT, early]):
        result = e.run("s01", ctx, overrides={"exit_signals": ["csgi_" + EXIT["id"], "csgi_early"]})
        assert result.exit_signal_hits[0]["signals"] == ["csgi_early"]
        assert result.exit_signal_hits[0]["signal_bar"] == 682
        ctx.positions[0]["available_qty"] = 0
        assert e.run("s01", ctx).exit_signal_hits == []
    with (custom_signals.private_signal_registry([ENTRY, {**EXIT, "enabled": False}]),
          pytest.raises(HTTPException, match="停用")):
        _validate_overlay_config({}, e.get("s01"), e)


def test_optional_daily_feature_plan_includes_signals_and_their_warmup():
    from app.backtest.minute_replay import minute_replay_feature_plan

    with custom_signals.private_signal_registry(ALL):
        plan = minute_replay_feature_plan(2, overrides={
            "entry_signals": ["signal_macd_golden", "signal_limit_up", "csg_daily_entry"],
            "exit_signals": ["csgi_other_exit"]})
    assert {"signal_macd_golden", "signal_limit_up", "csg_daily_entry"} <= plan.signal_columns
    assert {"macd_dif", "macd_dea"} <= plan.indicator_columns
    assert plan.warmup_bars >= 60


def test_generic_s02_templates_can_replace_s01_entry_and_exit():
    from test_late_session_strength import definition as late_signal
    from test_morning_liquidation_returns import signal as morning_signal

    e = StrategyEngine([BUILTINS])
    optional(e)
    late = late_signal(change_min_pct=0, change_max_pct=10, pool_up_min_pct=0,
                       ma5_1m_count=2, ma5_5m_count=2, evaluation_delay_minutes=.5)
    morning = morning_signal()
    ctx = context()
    ctx.history = ctx.history.with_columns(pl.Series(
        "close", [10. + i * .001 for i in range(ctx.history.height)])).with_columns(
        pl.col("close").alias("open"), (pl.col("close") + .01).alias("high"),
        (pl.col("close") - .01).alias("low"), (pl.col("close") * 100000).alias("amount"))
    with custom_signals.private_signal_registry([ENTRY, EXIT, late, morning]):
        result = e.run("s01", ctx, overrides={
            "entry_signals": ["csgi_late_session_strength"], "exit_signals": []})
        assert result.entry_signal_hits == [{"symbol": "600001.SH", "signals": ["csgi_late_session_strength"]}]
        ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                          "entry_unit_cost": 10., "entry_date": "2026-08-24", "sell_date": "2026-08-25"}]
        result = e.run("s01", ctx, overrides={"entry_signals": [], "exit_signals": ["csgi_morning_liquidation"]})
        assert result.exit_signal_hits[0]["signals"] == ["csgi_morning_liquidation"]
        assert result.exit_signal_hits[0]["signal_bar"] == 645


def test_generic_minute_trigger_receives_raw_previous_close():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context()
    ctx.history = ctx.history.with_columns(
        pl.when(pl.col("datetime") > datetime(2026, 8, 25, 10)).then(10.4)
        .otherwise(10.25).alias("close"))
    signal = {**MINUTE_ENTRY, "conditions": [{"left": "pct_vs_prev_close", "op": ">", "right": ".03"}]}
    with custom_signals.private_signal_registry([ENTRY, EXIT, signal]):
        result = e.run("s01", ctx, overrides={"entry_signals": ["csgi_other_entry"], "exit_signals": []})
    assert result.entry_signal_hits == [{"symbol": "600001.SH", "signals": ["csgi_other_entry"]}]
