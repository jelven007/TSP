"""Real cash replay follows template deadlines, never a fresh fallback window."""
from datetime import date, datetime, time, timedelta

import polars as pl
import pytest
from test_minute_backtest import _minute_frame
from test_morning_backtest import BUY, SELL, SYMBOL, configuration, service_fixture
from test_morning_returns_backtest import extended_frame, next_minute_run, volume

from app.strategy import custom_signals


def test_sale_continues_after_five_minutes_but_stops_before_fallback(tmp_path):
    result = next_minute_run(tmp_path, volume(extended_frame(), {"10:31": 1., "10:40": 3.}))
    assert result.error is None, result.error
    assert [fill["datetime"][-8:] for fill in result.fills if fill["side"] == "sell"] == [
        "10:31:00", "10:40:00"]
    assert not any(hit.get("reason") == "fallback" for hit in result.signals)


def test_fallback_has_no_capacity_at_or_after_its_clock(tmp_path):
    result = next_minute_run(tmp_path, volume(extended_frame(), {"10:31": 1., "10:45": 3., "10:50": 3.}))
    assert result.error and "10:45" in result.error
    assert [fill["datetime"][-8:] for fill in result.fills if fill["side"] == "sell"] == ["10:31:00"]
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300


def test_premarket_clock_trigger_waits_for_real_session_capacity(tmp_path):
    data = volume(_minute_frame(SELL, [(SYMBOL, "09:31", 10.4), (SYMBOL, "09:32", 10.4),
                                       (SYMBOL, "09:33", 10.4)]), {"09:31": 1., "09:32": 3., "09:33": 10.})
    subject, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]), SELL: data,
    })
    custom_signals.save_one(tmp_path, {
        "id": "morning_liquidation", "name": "早盘清仓", "kind": "exit",
        "template": "morning_liquidation", "timeframe": "intraday", "enabled": True,
        "conditions": [], "params": {
            "start_time": "07:00:00", "time": "08:00:15", "fallback_time": "09:33:00",
            "rise_pct": None, "fall_pct": None,
        },
    })
    result = subject.run(configuration(initial_capital=1000000, max_exposure_pct=.01))
    assert result.error is None, result.error
    assert result.trades[0]["exit_signal_date"].endswith("08:00:15")
    assert [fill["datetime"][-8:] for fill in result.fills if fill["side"] == "sell"] == [
        "09:31:00", "09:32:00"]


def test_s01_optional_morning_adapter_carries_the_actual_fallback_deadline():
    from test_s01_strategy import BUILTINS

    from app.strategy.engine import StrategyDataContext, StrategyEngine
    from app.strategy.s01_signals import optional_exit_hits

    engine = StrategyEngine([BUILTINS])
    strategy = engine.get("s01")
    sig = {
        "id": "exit", "column": "csgi_exit", "template": "morning_liquidation",
        "params": {"time": "10:30:00", "fallback_time": "13:07:30", "rise_pct": 6., "fall_pct": 6.},
    }
    ctx = StrategyDataContext("stock", "1m", SELL, history=pl.DataFrame(),
        daily_history=pl.DataFrame({"symbol": [SYMBOL], "date": [BUY], "raw_close": [10.]}),
        completed_through=datetime.combine(SELL, time(15)),
        positions=[{"symbol": SYMBOL, "entry_date": str(BUY), "qty": 100, "available_qty": 100}])
    hit, = optional_exit_hits(engine, strategy, ctx, {}, [sig])
    assert hit["first_eligible_bar"] == 631
    assert hit["last_eligible_bar"] == 787
    assert hit["template_deadline"] is True


def test_s01_common_cash_replay_uses_both_selected_template_deadlines(tmp_path):
    from test_s01_optional_triggers import optional
    from test_s01_strategy import _service

    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    subject = _service(tmp_path)
    strategy = optional(subject.strategy_engine)
    strategy.meta["params"] = []
    load = subject.engine.repo.get_minute_by_dates
    buy_day, sell_day = date(2026, 8, 25), date(2026, 8, 26)

    def limited(symbols, days, asset_type):
        data = load(symbols, days, asset_type)
        return (data.with_columns(pl.lit(1.).alias("volume")) if days[0] == buy_day
                else volume(data, {"13:06": 1., "13:07": 9., "13:08": 10000.}))

    subject.engine.repo.get_minute_by_dates = limited
    late = {
        "id": "late", "name": "尾盘走强", "kind": "entry", "template": "late_session_strength",
        "timeframe": "intraday", "enabled": True, "conditions": [], "params": {
            "time": "14:30:00", "end_time": "14:40:00", "evaluation_delay_minutes": .5,
            "change_min_pct": .1, "change_max_pct": None,
            "pool_up_min_pct": None, "ma5_1m_count": None, "ma5_5m_count": None,
        },
    }
    morning = {
        "id": "morning", "name": "早盘清仓", "kind": "exit", "template": "morning_liquidation",
        "timeframe": "intraday", "enabled": True, "conditions": [], "params": {
            "start_time": "13:00:00", "time": "13:05:15", "fallback_time": "13:07:30",
            "rise_pct": None, "fall_pct": None,
        },
    }
    with custom_signals.private_signal_registry([late, morning]):
        result = subject.run(StrategyBacktestConfig(
            "s01", None, buy_day, sell_day, initial_capital=1000000, max_exposure_pct=.1, max_positions=1,
            minute_price_rule="high_low_v1",
            overrides={"entry_signals": ["csgi_late"], "exit_signals": ["csgi_morning"],
                       "basic_filter": {"enabled": False}}),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None, result.error
    buys = [fill for fill in result.fills if fill["side"] == "buy"]
    assert len(buys) == 10 and buys[-1]["datetime"] == f"{buy_day} 14:40:00"
    assert [fill["datetime"][-8:] for fill in result.fills if fill["side"] == "sell"] == [
        "13:06:00", "13:07:00"]
    assert result.trades[0]["entry_signal_date"] == f"{buy_day} 14:30"
    assert not result.stats["execution"]["remaining_positions"]


def test_afternoon_second_precision_exit_skips_lunch_and_stops_at_saved_second(tmp_path):
    start = datetime.combine(SELL, time(9, 31))
    times = [start + timedelta(minutes=i) for i in range(120)]
    times += [datetime.combine(SELL, time(13, 1)) + timedelta(minutes=i) for i in range(10)]
    data = _minute_frame(SELL, [(SYMBOL, at.strftime("%H:%M"), 10.4) for at in times])
    data = volume(data, {"13:06": 1., "13:07": 3., "13:08": 10.})
    subject, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]), SELL: data,
    })
    custom_signals.save_one(tmp_path, {
        "id": "morning_liquidation", "name": "早盘清仓", "kind": "exit",
        "template": "morning_liquidation", "timeframe": "intraday", "enabled": True,
        "conditions": [], "params": {
            "start_time": "13:00:00", "time": "13:05:15", "fallback_time": "13:07:30",
            "rise_pct": None, "fall_pct": None,
        },
    })
    result = subject.run(configuration(initial_capital=1000000, max_exposure_pct=.01,
                                       exit_fill="signal_next_minute"))
    assert result.error is None, result.error
    assert result.trades[0]["exit_signal_date"].endswith("13:05:15")
    assert result.trades[0]["exit_date"] == f"{SELL} 13:07"


@pytest.mark.parametrize("end,expected", [("14:33:30", 3), ("14:40:00", 10)])
def test_late_template_buy_capacity_runs_until_saved_end(tmp_path, end, expected):
    buy = _minute_frame(BUY, [
        (SYMBOL, f"14:{minute:02d}", 10.2) for minute in range(30, 42)
    ]).with_columns(pl.lit(1.).alias("volume"))
    sells = _minute_frame(SELL, [(SYMBOL, f"10:{minute:02d}", 10.4) for minute in range(46)])
    sells = pl.concat([_minute_frame(SELL, [(SYMBOL, f"09:{minute}", 10.4)
                                          for minute in range(31, 60)]), sells])
    sells = pl.concat([sells, _minute_frame(SELL, [(SYMBOL, "14:30", 10.)])])
    subject, _, _ = service_fixture(tmp_path, frames={BUY: buy, SELL: sells})
    custom_signals.save_one(tmp_path, {
        "id": "late", "name": "尾盘走强", "kind": "entry", "template": "late_session_strength",
        "timeframe": "intraday", "enabled": True, "conditions": [], "params": {
            "time": "14:30:00", "end_time": end, "evaluation_delay_minutes": .5,
            "change_min_pct": .1, "change_max_pct": None,
            "pool_up_min_pct": None, "ma5_1m_count": None, "ma5_5m_count": None,
        },
    })
    result = subject.run(configuration(initial_capital=1000000, max_exposure_pct=.1,
        overrides={"entry_signals": ["csgi_late"], "exit_signals": ["csgi_morning_liquidation"],
                   "basic_filter": {"enabled": False}}))
    assert result.error is None, result.error
    fills = [fill for fill in result.fills if fill["side"] == "buy"]
    assert len(fills) == expected
    assert all(fill["datetime"] <= f"{BUY} {end}" for fill in fills)
