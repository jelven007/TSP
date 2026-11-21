from datetime import date

import polars as pl
import pytest
from test_minute_backtest import _daily_panel, _make_service, _minute_frame, _trading_days

from app.backtest.minute_replay import MinuteSignalReplayer
from app.strategy import custom_signals


@pytest.mark.parametrize("capital,expected", [(1_000_000., 1), (100_000_000., 0)])
def test_late_replay_uses_signal_day_capital_and_only_previous_day_prices(tmp_path, capital, expected):
    day, symbol = date(2026, 11, 11), "600001.SH"
    panel = _daily_panel(_trading_days(33, start=date(2026, 10, 1)), [symbol]).with_columns(
        pl.lit(10.).alias("close"), pl.lit(10.).alias("raw_close"),
        pl.when(pl.col("date") == day).then(capital).otherwise(4e8).alias("float_shares"))
    frames = {day: _minute_frame(day, [(symbol, "14:50", 10.4), (symbol, "14:51", 10.41)])}
    service = _make_service(tmp_path, panel, frames)
    (tmp_path / "strategies/test_minute_ping.py").write_text('''
import polars as pl
META = {"id": "test_minute_ping", "name": "s03", "asset_types": ["stock"], "timeframes": ["1m"],
        "daily_history_bars": 5, "result_sections": "signals"}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
ENTRY_SIGNALS = ["csgi_late"]
REQUIRED_FEATURES = ["csgi_late"]
def filter_minute_history(df, params, *, daily=None):
    assert daily["date"].max() < df["datetime"].min().date()
    return df.filter(pl.col("csgi_late")).rename({"datetime": "last_datetime"})
''')
    service.strategy_engine.reload()
    service.strategy_engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, {
        "id": "late", "name": "s03_尾盘建仓", "kind": "entry", "timeframe": "intraday",
        "template": "late_session_strength", "enabled": True, "conditions": [],
        "params": {"time": "14:50:00", "end_time": "15:00:00", "evaluation_delay_minutes": .5,
                   "turnover_min_pct": 5, "pool_up_min_pct": 60,
                   "change_min_pct": 3, "change_max_pct": 5,
                   "ma5_1m_count": None, "ma5_5m_count": None}})
    result = MinuteSignalReplayer(service.engine, service.strategy_engine).replay(
        service.strategy_engine.get("test_minute_ping"), panel=panel, start=day, end=day,
        params={}, overrides={"exit_signals": []})
    assert not result.errors
    assert len(result.hits) == expected
    if expected:
        assert result.hits[0].trigger_time == "14:51"


def test_all_price_qualified_candidates_missing_capital_are_not_successful_zero():
    from app.strategy.late_session_strength import evaluate

    day = date(2026, 11, 11)
    frame = _minute_frame(day, [("600001.SH", "14:50", 10.4), ("600002.SH", "14:50", 10.1)])
    signal = {
        "id": "late", "enabled": True, "template": "late_session_strength",
        "params": {"time": "14:50:00", "end_time": "15:00:00", "evaluation_delay_minutes": .5,
                   "turnover_min_pct": 5, "pool_up_min_pct": 60,
                   "change_min_pct": 3, "change_max_pct": 5,
                   "ma5_1m_count": None, "ma5_5m_count": None},
    }
    result = evaluate(frame, signal, day=day, pool_symbols=["600001.SH", "600002.SH"],
                      prev_close={"600001.SH": 10., "600002.SH": 10.},
                      float_shares={"600002.SH": 1e6})
    assert result.diagnostics
    assert result.stage == "waiting_data"
