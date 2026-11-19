"""New conservative prices remain separate from old frozen execution rules."""
from datetime import date

import polars as pl
import pytest
from test_minute_backtest import _daily_panel, _make_service, _minute_frame, _trading_days
from test_morning_backtest import BUY, SELL, SYMBOL, configuration, service_fixture
from test_morning_returns_backtest import extended_frame

from app.backtest.minute_replay import MinuteSignalReplayer
from app.backtest.overnight import fill_order
from app.backtest.worker import _decode_backtest_config
from app.strategy import custom_signals
from app.strategy.builtin._overnight import DEFAULTS


def test_old_frozen_config_without_price_rule_retains_legacy_prices():
    config = _decode_backtest_config({
        "strategy_id": "s03", "symbols": [SYMBOL], "start": "2026-06-29", "end": "2026-06-30",
    })
    assert config.minute_price_rule == "legacy"


def test_new_morning_rule_uses_each_minute_low_without_second_slippage(tmp_path):
    service, _, frames = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]), SELL: extended_frame(),
    })
    config = configuration(initial_capital=1000000, exit_fill="signal_next_minute", slippage_bps=100)
    config.minute_price_rule = "high_low_v1"
    result = service.run(config)
    assert not result.error, result.error
    sales = [fill for fill in result.fills if fill["side"] == "sell"]
    assert sales
    for fill in sales:
        low = frames[SELL].filter(
            pl.col("datetime").dt.strftime("%Y-%m-%d %H:%M:%S") == fill["datetime"],
        )["low"].item()
        assert fill["price"] == pytest.approx(low)
    assert result.config["minute_price_rule"] == "high_low_v1"


def test_explicit_overnight_low_is_not_discounted_again():
    frame = _minute_frame(date(2026, 6, 29), [(SYMBOL, "11:26", 10.4)])
    sale = fill_order(
        frame, "sell", SYMBOL, 10., 686, 686, {**DEFAULTS, "slippage": .01},
        quantity=100, price_field="low",
    )
    assert sale["fills"][0]["price"] == pytest.approx(frame["low"].item())


def test_june29_later_signal_keeps_its_minutes_with_an_earlier_required_signal(tmp_path):
    day = date(2026, 6, 29)
    panel = _daily_panel(_trading_days(70, start=date(2026, 4, 1)), [SYMBOL]).with_columns(
        pl.lit(10.).alias("close"), pl.lit(10.).alias("raw_close"),
    )
    frames = {day: _minute_frame(day, [
        (SYMBOL, "14:30", 10.3), (SYMBOL, "14:50", 10.4), (SYMBOL, "14:51", 10.41),
    ])}
    service = _make_service(tmp_path, panel, frames)
    (tmp_path / "strategies/test_minute_ping.py").write_text('''
import polars as pl
META = {"id": "test_minute_ping", "name": "s03", "asset_types": ["stock"], "timeframes": ["1m"]}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
ENTRY_SIGNALS = ["csgi_early"]
REQUIRED_FEATURES = ["csgi_early", "csgi_late"]
def filter_minute_history(df, params):
    return df.filter(pl.col("csgi_late")).rename({"datetime": "last_datetime"})
''')
    service.strategy_engine.reload()
    service.strategy_engine._user_data_dir = lambda: tmp_path
    empty = dict.fromkeys(("end_time", "change_min_pct", "change_max_pct", "amount_min_100m",
                          "turnover_min_pct", "amplitude_max_pct", "volume_ratio_min",
                          "ma5_1m_count", "ma5_5m_count"))
    for sid, at in (("early", "14:30:00"), ("late", "14:50:00")):
        custom_signals.save_one(tmp_path, {
            "id": sid, "name": sid, "kind": "entry", "timeframe": "intraday",
            "template": "late_session_strength", "conditions": [], "enabled": True,
            "params": {**empty, "time": at, "evaluation_delay_minutes": .5, "pool_up_min_pct": 60},
        })
    replay = MinuteSignalReplayer(service.engine, service.strategy_engine).replay(
        service.strategy_engine.get("test_minute_ping"), panel=panel, start=day, end=day,
        params={}, overrides={"entry_signals": ["csgi_late"]}, symbols=[SYMBOL],
    )
    assert not replay.errors, replay.errors
    assert len(replay.hits) == 1
    assert replay.hits[0].trigger_time == "14:51"
