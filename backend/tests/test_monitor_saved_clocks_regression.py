from datetime import datetime

from app.market_time import CN_TZ
from app.services.strategy_monitor_tasks import monitoring_summary, set_monitoring, task_windows
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine


def test_summary_shows_saved_clock_and_keeps_real_time_execution_cutoff(tmp_path):
    (tmp_path / "strategy.py").write_text('''
import polars as pl
META = {"id": "custom_test", "name": "s03", "timeframes": ["1m"]}
EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = ["csgi_late"]
def filter_minute_history(df, params):
    return df
''')
    signal = {
        "id": "late", "name": "尾盘建仓", "kind": "entry", "timeframe": "intraday",
        "enabled": True, "template": "late_session_strength", "conditions": [],
        "params": {"time": "14:50:00", "end_time": "15:00:00",
                   "evaluation_delay_minutes": .5, "pool_up_min_pct": None,
                   "change_min_pct": None, "change_max_pct": None,
                   "ma5_1m_count": None, "ma5_5m_count": None},
    }
    custom_signals.save_one(tmp_path, signal)
    engine = StrategyEngine([tmp_path])
    engine._user_data_dir = lambda: tmp_path
    set_monitoring(tmp_path, "custom_test", "s03", True)
    now = datetime(2026, 11, 20, 14, 56, tzinfo=CN_TZ)
    task = monitoring_summary(engine, tmp_path, now=now)["strategies"][0]["tasks"]["buy"]
    assert task["windows"] == [{"start": "14:50:00", "end": "15:00:00"}]
    assert task["execution_deadline"] == "14:55:00"
    assert task["state"] == "expired"
    assert task_windows(engine, engine.get("custom_test"), {})["buy"][0]["end"] == "14:55:00"
    custom_signals.save_one(tmp_path, {
        **signal, "params": {**signal["params"], "time": "14:40:00", "end_time": "14:52:00"}})
    updated = monitoring_summary(engine, tmp_path, now=now)["strategies"][0]["tasks"]["buy"]
    assert updated["windows"] == [{"start": "14:40:00", "end": "14:52:00"}]
    assert updated.get("execution_deadline") is None
