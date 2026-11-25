from datetime import date, datetime

import polars as pl

from app.market_time import CN_TZ
from app.services.strategy_monitor_state import plan, resume_confirmation_timeout
from app.strategy.late_session_strength import evaluate

DAY = date(2026, 11, 23)
SIGNAL = {"id": "late", "template": "late_session_strength",
          "optional_parameters": True, "params": {
              "time": "14:30:00", "end_time": "14:55:00",
              "evaluation_delay_minutes": .5, "pool_up_min_pct": 60,
              "ma5_1m_count": None, "ma5_5m_count": None,
              "change_min_pct": None, "change_max_pct": None}}
WINDOWS = {"buy": [{"start": "14:30:00", "end": "14:55:00", "signal": SIGNAL}], "sell": []}


def test_breadth_can_retry_after_earliest_confirmation_before_entry_deadline():
    tasks = {"buy": {"signals": {"late": {"breadth": {"state": "failed", "terminal": False}}}}}
    actions = plan(WINDOWS, tasks, datetime(2026, 11, 23, 14, 31, tzinfo=CN_TZ), trading=True)["actions"]
    assert actions[0]["phase"] == "breadth"
    assert actions[0]["retry_until"] == "2026-11-23T14:55:00+08:00"
    assert plan(WINDOWS, tasks, datetime(2026, 11, 23, 14, 56, tzinfo=CN_TZ), trading=True)["actions"][0]["phase"] == "expire"


def test_missing_target_minute_recovers_after_confirmation_without_using_later_quotes():
    state = {}
    empty = pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime, "close": pl.Float64})
    first = evaluate(empty, SIGNAL, day=DAY, pool_symbols=["600001.SH"],
                     prev_close={"600001.SH": 10.}, task_state=state, breadth_only=True,
                     completed_through=datetime(2026, 11, 23, 14, 30, 20))
    assert first.diagnostics
    frame = pl.DataFrame({"symbol": ["600001.SH", "600001.SH"],
                          "datetime": [datetime(2026, 11, 23, 14, 30), datetime(2026, 11, 23, 14, 31)],
                          "close": [10.5, 9.]})
    recovered = evaluate(frame, SIGNAL, day=DAY, pool_symbols=["600001.SH"],
                         prev_close={"600001.SH": 10.}, task_state=state, breadth_only=True,
                         completed_through=datetime(2026, 11, 23, 14, 32))
    assert not recovered.diagnostics
    assert recovered.pool_up_pct == 100
    assert recovered.progress["bar_at"] == "2026-11-23T14:30:00"


def test_old_confirmation_expiry_resumes_once_without_erasing_audit():
    old = {"state": "expired", "terminal": True, "retry_until": "2026-11-23T14:30:30+08:00",
           "history": [{"state": "failed", "errors": ["分钟数据缺失"]}, {"state": "expired"}],
           "first_error": "分钟数据缺失"}
    tasks = {"buy": {"state": "expired", "terminal": True,
                     "signals": {"late": {"state": "expired", "terminal": True, "breadth": old}}}}
    now = datetime(2026, 11, 23, 14, 32, tzinfo=CN_TZ)
    resumed = resume_confirmation_timeout(WINDOWS, tasks, now)
    assert resumed["buy"]["terminal"] is False
    assert resumed["buy"]["signals"]["late"]["breadth"]["history"] == old["history"]
    assert resumed["buy"]["signals"]["late"]["breadth"]["first_error"] == old["first_error"]
    assert tasks["buy"]["terminal"] is True
    assert resume_confirmation_timeout(WINDOWS, resumed, now) == resumed
    assert resume_confirmation_timeout(WINDOWS, tasks, now.replace(hour=15)) == tasks
    tasks["buy"]["published"] = True
    assert resume_confirmation_timeout(WINDOWS, tasks, now) == tasks
