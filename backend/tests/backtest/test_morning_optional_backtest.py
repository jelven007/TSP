"""Nullable signal clocks never become synthetic fallback events in the cash ledger."""
import polars as pl
import pytest
from test_minute_backtest import _minute_frame
from test_morning_backtest import BUY, SELL, SYMBOL, configuration, service_fixture
from test_morning_returns_backtest import extended_frame, volume

from app.strategy import custom_signals

EMPTY = dict.fromkeys(("start_time", "rise_pct", "fall_pct", "decline_3m_pct",
                      "volume_ratio_min", "time", "fallback_time"))


def run_optional(tmp_path, params, data=None):
    service, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]),
        SELL: extended_frame() if data is None else data,
    })
    custom_signals.save_one(tmp_path, {
        "id": "morning_liquidation", "name": "早盘清仓", "kind": "exit",
        "timeframe": "intraday", "template": "morning_liquidation", "conditions": [],
        "enabled": True, "params": {**EMPTY, **params},
    })
    return service.run(configuration(initial_capital=1000000, max_exposure_pct=.01,
                                     exit_fill="signal_next_minute"))


@pytest.mark.parametrize("key,reason,at", [
    ("time", "stop_time", "10:30:00"), ("fallback_time", "fallback", "10:45:00"),
])
def test_single_enabled_clock_has_only_its_own_stage(tmp_path, key, reason, at):
    result = run_optional(tmp_path, {key: at})
    assert not result.error, result.error
    exits = [hit for hit in result.signals if hit["side"] == "sell"]
    assert [(hit["reason"], hit["trigger_time"][-8:]) for hit in exits] == [(reason, at)]


def test_disabled_fallback_cannot_supply_a_second_capacity_window(tmp_path):
    result = run_optional(tmp_path, {"time": "10:30:00"},
                          volume(extended_frame(), {"10:31": 1., "10:46": 10000.}))
    assert result.error and "10:35" in result.error and "余股" in result.error
    assert [hit["reason"] for hit in result.signals if hit["side"] == "sell"] == ["stop_time"]
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300


def test_early_only_uses_real_signal_and_no_clock_stage(tmp_path):
    data = extended_frame().with_columns(*[
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "10:00")
        .then(10.6).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")])
    result = run_optional(tmp_path, {"rise_pct": 6}, data)
    assert not result.error, result.error
    assert [hit["reason"] for hit in result.signals if hit["side"] == "sell"] == ["rise"]
    assert result.trades[0]["exit_date"] == f"{SELL} 10:01"


def test_all_empty_is_explicitly_rejected_before_creating_cash_ledger(tmp_path):
    result = run_optional(tmp_path, {})
    assert result.error and "全部参数为空" in result.error
    assert not result.fills and not result.equity_curve


@pytest.mark.parametrize("key", ["start_time", "time", "fallback_time"])
def test_only_present_clocks_require_whole_minutes_for_cash_backtest(tmp_path, key):
    result = run_optional(tmp_path, {key: "10:00:15", "rise_pct": 6})
    assert result.error and "整分钟" in result.error
    assert not result.fills
