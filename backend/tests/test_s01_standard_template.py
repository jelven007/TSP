"""Common S01 controls must reach saved settings, causal entries and capacity exits."""
from datetime import date, datetime

import polars as pl
import pytest
from fastapi import HTTPException
from test_s01_optional_triggers import ALL, context, optional
from test_s01_strategy import BUILTINS, _service, bars

from app.api.strategy import _validate_overlay_config
from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine


def controls(**values):
    return {"entry_signals": [], "exit_signals": [], **values}


def test_common_controls_save_but_legacy_specialized_strategy_keeps_rejection():
    e = StrategyEngine([BUILTINS])
    values = controls(overlay_filter=[{"left": "close", "op": ">", "right": "9"}],
                      scoring={"change_pct": 1}, stop_loss=-.06)
    with custom_signals.private_signal_registry(ALL):
        with pytest.raises(HTTPException):
            _validate_overlay_config(values, e.get("s01"), e)
        _validate_overlay_config(values, optional(e), e)


def test_optional_overlay_uses_previous_close_and_never_changes_full_pool():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context()
    ctx.daily_history = pl.concat([ctx.daily_history, ctx.daily_history.tail(1).with_columns(
        pl.lit(ctx.as_of).alias("date"), pl.lit(100.).alias("close"))])
    with custom_signals.private_signal_registry(ALL):
        result = e.run("s01", ctx, overrides=controls(
            overlay_filter=[{"left": "close", "op": ">", "right": "20"}]))
    assert result.total == 0
    assert result.daily_stock_pool["total"] == 1


def test_daily_rolling_score_controls_actual_candidate_order():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context()
    days = pl.date_range(date(2026, 7, 20), date(2026, 8, 24), eager=True).to_list()
    source = ctx.daily_history.tail(1).drop("date")
    ctx.daily_history = pl.concat([
        source.with_columns(pl.lit(day).alias("date"), pl.lit(turnover).alias("turnover_rate"),
                            pl.lit(symbol).alias("symbol"),
                            pl.lit(10. if day == date(2026, 8, 24) else 9.9).alias("raw_close"))
        for day in days for symbol, turnover in [("600001.SH", 5.), ("600002.SH", 2.)]
    ])
    ctx.current = ctx.daily_history.filter(pl.col("date") == date(2026, 8, 24))
    ctx.history = pl.concat([ctx.history, ctx.history.with_columns(pl.lit("600002.SH").alias("symbol"))])
    values = controls(scoring={"turnover_mean_20d": 1},
                      scoring_directions={"turnover_mean_20d": "low"})
    with custom_signals.private_signal_registry(ALL):
        result = e.run("s01", ctx, overrides=values)
    assert [r["symbol"] for r in result.rows] == ["600002.SH", "600001.SH"]
    assert [r["score"] for r in result.rows] == [100., 0.]
    assert e.minute_daily_history_bars(["s01"], overrides_map={"s01": values}) >= 21


@pytest.mark.parametrize("control, price, reason", [
    ({"stop_loss": -.06}, 9.3, "stop_loss"),
    ({"take_profit": .1}, 11.1, "take_profit"),
    ({"trailing_stop": -.05}, 9.3, "trailing_stop"),
])
def test_price_control_exit_is_completed_t1_and_independent_of_entries(control, price, reason):
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=573)
    ctx.history = bars(price=price, end=574)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10., "entry_date": "2026-08-24",
                      "sell_date": "2026-08-25", "risk_peak": 10.}]
    with custom_signals.private_signal_registry(ALL):
        result = e.run("s01", ctx, overrides=controls(**control))
        hit = result.exit_signal_hits[0]
        assert hit["reason"] == reason
        assert hit["signal_bar"] == 571
        assert hit["first_eligible_bar"] == 572
        ctx.positions[0]["available_qty"] = 0
        assert e.run("s01", ctx, overrides=controls(**control)).exit_signal_hits == []


def test_peak_uses_previous_minute_and_price_protection_precedes_profit():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=573)
    ctx.history = bars(price=10., end=574).with_columns(
        pl.when(pl.col("datetime") == datetime(2026, 8, 25, 9, 31)).then(12.).otherwise(10.).alias("high"),
        pl.lit(9.9).alias("low"))
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10., "entry_date": "2026-08-24",
                      "sell_date": "2026-08-25", "risk_peak": 10.}]
    with custom_signals.private_signal_registry(ALL):
        result = e.run("s01", ctx, overrides=controls(
            trailing_take_profit_activate=.1, trailing_take_profit_drawdown=.05))
        assert result.exit_signal_hits[0]["signal_bar"] == 572
        result = e.run("s01", ctx, overrides=controls(stop_loss=-.005, take_profit=.1))
        assert result.exit_signal_hits[0]["reason"] == "stop_loss"


def test_max_hold_counts_confirmed_sessions_and_does_not_need_price():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=573)
    ctx.history = pl.DataFrame()
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10., "entry_date": "2026-08-21",
                      "sell_date": "2026-08-24"}]
    with custom_signals.private_signal_registry(ALL):
        assert e.run("s01", ctx, overrides=controls(max_hold_days=3)).exit_signal_hits == []
        hit = e.run("s01", ctx, overrides=controls(max_hold_days=2)).exit_signal_hits[0]
    assert hit["reason"] == "max_hold"
    assert hit["signal_bar"] == 570


def test_backtest_risk_setting_reaches_real_next_minute_capacity_sales(tmp_path):
    service = _service(tmp_path)
    optional(service.strategy_engine)
    repo = service.engine.repo
    original = repo.get_minute_by_dates
    repo.get_minute_by_dates = lambda symbols, days, asset: (
        bars(day=days[0], end=890, price=10.5) if days[0] == date(2026, 8, 26)
        else original(symbols, days, asset))
    with custom_signals.private_signal_registry(ALL):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26),
            params={"initial_cash": 1000000},
            overrides=controls(take_profit=.01)),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert result.fills[0]["side"] == "buy"
    assert result.signals[0]["reason"] == "take_profit"
    assert result.fills[-1]["side"] == "sell"
    assert result.fills[-1]["bar"] > result.signals[0]["signal_bar"]


def test_trailing_reconstructs_prior_peak_only_after_the_buy_minute():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=573)
    ctx.history = bars(price=10., end=574)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_unit_cost": 10., "entry_date": "2026-08-24",
                      "entry_time": "2026-08-24 14:47:00", "sell_date": "2026-08-25"}]
    prior = bars(day=date(2026, 8, 24), end=900, price=10.)
    ctx.holding_history_loader = lambda *_: prior.with_columns(
        pl.when(pl.col("datetime") <= datetime(2026, 8, 24, 14, 47))
        .then(100.).otherwise(pl.col("high")).alias("high"))
    with custom_signals.private_signal_registry(ALL):
        assert e.run("s01", ctx, overrides=controls(trailing_stop=-.05)).exit_signal_hits == []
        ctx.holding_history_loader = lambda *_: prior.with_columns(
            pl.when(pl.col("datetime") == datetime(2026, 8, 24, 14, 48))
            .then(11.).otherwise(pl.col("high")).alias("high"))
        hit = e.run("s01", ctx, overrides=controls(trailing_stop=-.05)).exit_signal_hits[0]
    assert hit["reason"] == "trailing_stop"
    assert hit["signal_bar"] == 571


def test_missing_risk_cost_and_trailing_history_report_failure():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=573)
    ctx.positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                      "entry_date": "2026-08-24", "entry_time": "2026-08-24 14:47:00",
                      "sell_date": "2026-08-25"}]
    with custom_signals.private_signal_registry(ALL):
        result = e.run("s01", ctx, overrides=controls(stop_loss=-.06))
        assert "成本" in result.exit_filter_error
        ctx.positions[0]["entry_unit_cost"] = 10.
        ctx.holding_history_loader = lambda *_: bars(day=date(2026, 8, 24), end=890)
        result = e.run("s01", ctx, overrides=controls(trailing_stop=-.05))
        assert "分钟" in result.exit_filter_error


def test_full_optional_s01_captures_authorized_cost_and_clock_snapshot(monkeypatch):
    from types import SimpleNamespace

    from app.multitenancy.strategies import TenantStrategyEngine
    from app.strategy import overnight_runtime

    e = StrategyEngine([BUILTINS])
    optional(e)
    positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                  "entry_unit_cost": 10., "entry_time": "2026-08-24 14:47:00"}]
    calls = []
    monkeypatch.setattr(overnight_runtime, "position_snapshot",
                        lambda storage, sid, day: calls.append((sid, day)) or positions)
    result = TenantStrategyEngine._holding_positions(e,
        SimpleNamespace(connection=None), {"strategy_ids": ["s01"], "as_of": "2026-08-25",
                                          "signals": ALL, "overrides": {"s01": controls()}})
    assert result == {"s01": positions}
    assert calls == [("s01", date(2026, 8, 25))]


def test_monitor_uses_frozen_capacity_holdings_even_without_morning_template():
    from types import SimpleNamespace

    from app.strategy.overnight_runtime import monitor_context

    e = StrategyEngine([BUILTINS])
    optional(e)
    positions = [{"symbol": "600001.SH", "qty": 100, "available_qty": 100,
                  "entry_unit_cost": 10., "entry_date": "2026-08-24", "sell_date": "2026-08-25"}]
    e._position_loader = lambda *_: positions
    e._override_loader = lambda *_: controls(stop_loss=-.06)

    class Loader:
        repo = SimpleNamespace(get_minute_by_dates=lambda *_: pl.DataFrame())

        def build_strategy_context(self, *args, **kwargs):
            return context(end=573)

    loader = Loader()
    with custom_signals.private_signal_registry(ALL):
        ctx = monitor_context(loader.build_strategy_context, e, "s01", date(2026, 8, 25),
                              pl.DataFrame(), None)
    assert ctx.positions == positions


def test_late_risk_sale_cannot_fund_or_free_a_slot_for_an_earlier_entry(tmp_path):
    service = _service(tmp_path)
    optional(service.strategy_engine)
    panel = service.engine.load_panel_for_backtest()
    last = date(2026, 8, 27)
    panel = pl.concat([panel, panel.tail(1).with_columns(pl.lit(last).alias("date"))])
    service.engine.load_panel_for_backtest = lambda *_a, **_k: panel
    repo = service.engine.repo
    repo.list_minute_dates = lambda *_: [date(2026, 8, d) for d in (25, 26, 27)]
    repo.get_minute_by_dates = lambda symbols, days, asset: bars(
        day=days[0], price=10.25, end=900).with_columns(*[
            pl.when((pl.col("datetime").dt.date() == date(2026, 8, 26))
                    & (pl.col("datetime") >= datetime(2026, 8, 26, 14, 50)))
            .then(9.2).otherwise(pl.col(key)).alias(key)
            for key in ("open", "high", "low", "close")])
    with custom_signals.private_signal_registry(ALL):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), last,
            params={"initial_cash": 1000000, "position_limit": 1, "prior_return_min": 0},
            overrides=controls(stop_loss=-.06)),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert result.signals[0]["signal_bar"] == 890
    assert not [f for f in result.fills if f["side"] == "buy" and f["date"] == "2026-08-26"]
