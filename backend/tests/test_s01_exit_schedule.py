"""Approved S01 clocks must control signals and the actual remaining-share ledger."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest
from test_s01_equal_selected import SECOND, START, service
from test_s01_optional_triggers import context, optional
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import BUILTINS, bars

from app.backtest.overnight import order_fee
from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig
from app.strategy import custom_signals
from app.strategy.builtin._overnight import exit_signal
from app.strategy.engine import StrategyEngine
from app.strategy.s01_signals import signal_parameters

SCHEDULE = {
    **{k: v for k, v in EXIT["params"].items() if k not in ("guard_time", "sell_deadline")},
    "start_time": "11:15:00", "stop_time": "11:26:00", "fallback_time": "13:30:00",
}
NEW_EXIT = {**EXIT, "params": SCHEDULE}


def test_new_clock_contract_keeps_all_six_thresholds():
    assert signal_parameters(NEW_EXIT) == SCHEDULE
    custom_signals.validate(NEW_EXIT)
    assert signal_parameters(EXIT) == EXIT["params"]


@pytest.mark.parametrize("patch", [
    {"start_time": "11:26:00"}, {"stop_time": "11:15:00"},
    {"fallback_time": "12:30:00"}, {"fallback_time": "15:00:00"},
    {"stop_time": "11:26:01"}, {"start_time": "11:15"},
    {"stop_time": None}, {"guard_time": "11:22"},
])
def test_invalid_new_clocks_rejected(patch):
    with pytest.raises(ValueError):
        signal_parameters({**NEW_EXIT, "params": {**SCHEDULE, **patch}})


def test_before_start_has_no_exit_and_start_evaluates_original_conditions():
    frame = bars(end=690, price=10).with_columns(
        pl.when(pl.col("datetime") == datetime(2026, 8, 25, 11, 15))
        .then(9.96).otherwise(pl.col("close")).alias("close"))
    assert exit_signal(frame, 674, 10, SCHEDULE) is None
    hit = exit_signal(frame, 675, 10, SCHEDULE)
    assert hit["reason"] == "morning_vwap_or_trailing"
    assert hit["first_eligible_bar"] == 676
    assert hit["exit_stage"] == "conditional"


@pytest.mark.parametrize("price,cost", [(11., 10.), (9.5, 10.), (10., None)])
def test_stop_clears_profit_loss_and_unknown_cost(price, cost):
    hit = exit_signal(bars(end=690, price=price), 686, cost, SCHEDULE)
    assert (hit["signal_bar"], hit["reason"], hit["exit_stage"]) == (686, "time_stop", "stop")
    assert (hit["first_eligible_bar"], hit["last_eligible_bar"]) == (687, 781)


def test_missing_quotes_cannot_block_either_unconditional_clock():
    assert exit_signal(pl.DataFrame(), 685, None, SCHEDULE) is None
    stop = exit_signal(pl.DataFrame(), 686, None, SCHEDULE)
    fallback = exit_signal(pl.DataFrame(), 810, None, SCHEDULE, stop)
    assert stop["reason"] == "time_stop"
    assert fallback["reason"] == "time_fallback"
    assert (fallback["signal_bar"], fallback["first_eligible_bar"], fallback["last_eligible_bar"]) == (810, 811, 815)


def test_pending_conditional_sale_rearms_at_stop_then_fallback():
    pending = {"signal_bar": 675, "first_eligible_bar": 676, "last_eligible_bar": 680,
               "reason": "morning_vwap_or_trailing", "exit_stage": "conditional"}
    assert exit_signal(pl.DataFrame(), 685, None, SCHEDULE, pending) == pending
    stop = exit_signal(pl.DataFrame(), 686, None, SCHEDULE, pending)
    assert stop["signal_bar"] == 686
    assert exit_signal(pl.DataFrame(), 809, None, SCHEDULE, stop) == stop
    assert exit_signal(pl.DataFrame(), 810, None, SCHEDULE, stop)["signal_bar"] == 810
    assert exit_signal(pl.DataFrame(), 810, None, EXIT["params"], pending) == pending


def test_runtime_emits_independent_clock_events_for_remaining_cycles():
    engine = StrategyEngine([BUILTINS])
    optional(engine)
    ctx = context(end=815, price=11)
    ctx.history = pl.DataFrame()
    ctx.positions = [
        {"symbol": "600001.SH", "account_id": account, "cycle_id": account,
         "qty": 100, "available_qty": available, "entry_unit_cost": None,
         "entry_date": entry, "sell_date": "2026-08-25"}
        for account, available, entry in [("a", 100, "2026-08-24"),
                                         ("b", 100, "2026-08-24"),
                                         ("t0", 100, "2026-08-25"),
                                         ("sold", 0, "2026-08-24")]
    ]
    with custom_signals.private_signal_registry([NEW_EXIT]):
        result = engine.run("s01", ctx, overrides={"entry_signals": [], "exit_signals": ["csgi_" + EXIT["id"]]})
    assert {(h["account_id"], h["signal_bar"]) for h in result.exit_signal_hits} == {
        ("a", 686), ("a", 810), ("b", 686), ("b", 810)}
    assert all(h["signals"] == ["csgi_" + EXIT["id"]] for h in result.exit_signal_hits)
    assert all(h["holding_exit"] for h in result.exit_signal_hits)


def test_current_custom_source_accepts_new_signal_without_changing_entry_parameters(tmp_path):
    source = Path(__file__).resolve().parent / "fixtures/s01_available_minutes.py"
    (tmp_path / "s01.py").write_text(source.read_text())
    engine = StrategyEngine([tmp_path])
    assert engine.get("custom_mvxxcs01").meta["name"] == "s01"
    ctx = context(end=815, price=11)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10, "entry_date": "2026-08-24", "sell_date": "2026-08-25"}]
    with custom_signals.private_signal_registry([NEW_EXIT]):
        result = engine.run("custom_mvxxcs01", ctx, overrides={
            "entry_signals": [], "exit_signals": ["csgi_" + EXIT["id"]]})
    assert [h["signal_bar"] for h in result.exit_signal_hits] == [686, 810]


def test_new_and_frozen_exit_selections_keep_independent_clock_semantics():
    engine = StrategyEngine([BUILTINS])
    optional(engine)
    ctx = context(end=815, price=11)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_date": "2026-08-24", "entry_unit_cost": 10, "sell_date": "2026-08-25"}]
    legacy = {**EXIT, "id": "legacy", "params": {**EXIT["params"], "guard_time": "11:22"}}
    with custom_signals.private_signal_registry([NEW_EXIT, legacy]):
        result = engine.run("s01", ctx, overrides={
            "entry_signals": [], "exit_signals": ["csgi_" + EXIT["id"], "csgi_legacy"]})
    assert result.exit_signal_hits[0]["signal_bar"] == 682
    assert result.exit_signal_hits[0]["signals"] == ["csgi_legacy"]
    assert [h["signal_bar"] for h in result.exit_signal_hits if h.get("exit_stage")] == [686, 810]


def test_monitor_notifies_each_exit_phase_once_for_the_same_stock():
    from app.strategy.monitor import MonitorRuleEngine

    monitor = MonitorRuleEngine.__new__(MonitorRuleEngine)
    monitor._strategy_signal_state = {}
    monitor._strategy_signal_seen = {}
    key = ("rule", "custom_mvxxcs01", "stock")
    day = str(SECOND)
    hit = {"symbol": "600001.SH", "account_id": "a", "cycle_id": "a",
           "signals": ["csgi_" + EXIT["id"]]}
    conditional = {**hit, "exit_stage": "conditional", "signal_bar": 675}
    stop = {**hit, "exit_stage": "stop", "signal_bar": 686}
    fallback = {**hit, "exit_stage": "fallback", "signal_bar": 810}
    assert monitor._new_strategy_signals(key, "sell_signal", day, []) == set()
    for event, history in [(conditional, []), (stop, [conditional]), (fallback, [conditional, stop])]:
        assert monitor._new_strategy_signals(key, "sell_signal", day, [*history, event]) == {"600001.SH"}
        assert monitor._new_strategy_signals(key, "sell_signal", day, [*history, event]) == set()


def scheduled_replay(tmp_path, *, fallback_volume=1e6, conditional=False, missing_morning=False,
                     exit_fill="signal_next_minute", legacy_binding=False, exit_definition=NEW_EXIT):
    subject = service(tmp_path, 1)
    subject.strategy_engine.get("s01").meta["params"] = []
    if legacy_binding:
        strategy = subject.strategy_engine.get("s01")
        strategy.meta.pop("trigger_selection")
        strategy.meta["parameter_signals"] = {"entry": ENTRY["id"], "exit": EXIT["id"]}
    original = subject.engine.repo.get_minute_by_dates

    def limited(requested, days, asset):
        frame = original(requested, days, asset)
        if days[0] == SECOND:
            frame = frame.with_columns((pl.col("volume") * 100 * pl.col("close")).alias("amount"))
            bar = pl.col("datetime").dt.hour().cast(pl.Int32) * 60 + pl.col("datetime").dt.minute()
            frame = frame.with_columns(
                pl.when(bar.is_between(676, 690) | (bar == 781)).then(1.)
                .when(bar.is_between(811, 815)).then(fallback_volume)
                .otherwise(pl.col("volume")).alias("volume"))
            if conditional:
                frame = frame.with_columns(
                    pl.when(bar == 675).then(10.04).otherwise(pl.col("close")).alias("close"))
            if missing_morning:
                frame = frame.filter(bar >= 686)
        return frame

    subject.engine.repo.get_minute_by_dates = limited
    entry = {**ENTRY, "params": {**ENTRY["params"], "current_return_min": 0}}
    with custom_signals.private_signal_registry([entry, deepcopy(exit_definition)]):
        return subject.run(StrategyBacktestConfig(
            "s01", None, START, SECOND, params={"prior_return_min": 0, **(
                {"initial_cash": 1000000, "participation": .1, "position_limit": 1} if legacy_binding else {})},
            overrides={"entry_signals": ["csgi_" + ENTRY["id"]] if legacy_binding else [],
                       "exit_signals": ["csgi_" + EXIT["id"]]},
            initial_capital=1000000, max_positions=1, exit_fill=exit_fill,
            commission_pct=.00025, minimum_commission=5, transfer_fee_pct=.00001,
            stamp_tax_pct=.0005, slippage_bps=0),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))


@pytest.mark.parametrize("conditional", [False, True])
def test_partial_morning_sale_retries_and_completes_at_fallback(tmp_path, conditional):
    result = scheduled_replay(tmp_path, conditional=conditional)
    assert result.error is None
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert sells[0]["bar"] == (676 if conditional else 687)
    assert any(f["bar"] == 811 for f in sells)
    assert not any(782 <= f["bar"] <= 810 for f in sells)
    assert [h["signal_bar"] for h in result.signals] == ([675, 686, 810] if conditional else [686, 810])
    trade = result.trades[0]
    assert trade["exit_value"] == pytest.approx(sum(f["notional"] - f["fee"] for f in sells))
    assert trade["exit_price"] == pytest.approx(sum(f["notional"] for f in sells) / trade["shares"])
    assert sum(f["fee"] for f in sells) == pytest.approx(
        order_fee(sum(f["notional"] for f in sells), "sell", result.config["params"]))
    assert result.stats["execution"]["cash"] == pytest.approx(
        1000000 + sum((1 if f["side"] == "sell" else -1) * f["notional"] - f["fee"] for f in result.fills))
    assert not result.stats["execution"]["remaining_positions"]


def test_fallback_residual_rejects_nav_with_actual_cash_and_shares(tmp_path):
    result = scheduled_replay(tmp_path, fallback_volume=1)
    assert result.error and "13:35到期" in result.error
    assert not result.equity_curve
    assert not result.trades
    execution = result.stats["execution"]
    assert execution["remaining_positions"][0]["remaining"] == (
        sum(f["shares"] for f in result.fills if f["side"] == "buy")
        - sum(f["shares"] for f in result.fills if f["side"] == "sell"))
    assert execution["cash"] == pytest.approx(
        1000000 + sum((1 if f["side"] == "sell" else -1) * f["notional"] - f["fee"] for f in result.fills))


def test_missing_indicator_morning_does_not_block_real_clock_sale(tmp_path):
    result = scheduled_replay(tmp_path, missing_morning=True)
    assert result.error is None
    assert result.signals[0]["signal_bar"] == 686


@pytest.mark.parametrize("missing_morning", [False, True])
def test_new_clock_signal_also_executes_when_bound_by_legacy_source(tmp_path, missing_morning):
    result = scheduled_replay(tmp_path, legacy_binding=True, missing_morning=missing_morning)
    assert result.error is None
    assert [h["signal_bar"] for h in result.signals] == [686, 810]
    assert any(f["side"] == "sell" and f["bar"] == 811 for f in result.fills)


def test_non_minute_execution_choice_retains_its_explicit_semantics(tmp_path):
    result = scheduled_replay(tmp_path, exit_fill="close_t")
    assert result.error and "15:00到期" in result.error  # fixture has no closing minute
    assert len(result.signals) == 1
