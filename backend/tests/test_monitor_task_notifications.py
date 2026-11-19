from copy import deepcopy
from datetime import datetime

from app.market_time import CN_TZ
from app.multitenancy.monitor_compute import failure_events

RULES = [{"id": "mr", "strategy_id": "custom_any", "name": "自定义策略"}]
NOW = datetime(2026, 11, 20, 14, 31, tzinfo=CN_TZ)


def daily(task):
    return {"custom_any": {"as_of": "2026-11-20", "runtime_identity": "v1",
                            "monitor_tasks": {"buy": task}}}


def test_retries_and_runtime_refresh_do_not_create_new_task_notifications():
    result = daily({"state": "failed", "attempts": 1, "errors": ["缺分钟"],
                    "retry_until": "2026-11-20T14:55:00+08:00"})
    events, state = failure_events(result, RULES, {}, NOW)
    assert len(events) == 1
    result["custom_any"]["runtime_identity"] = "v2"
    task = result["custom_any"]["monitor_tasks"]["buy"]
    task.update(attempts=40, completed_at="2026-11-20T14:32:00+08:00")
    assert failure_events(result, RULES, state, NOW)[0] == []
    task.update(state="success", terminal=True, errors=[])
    recovered, _ = failure_events(result, RULES, state, NOW)
    assert len(recovered) == 1
    assert recovered[0]["type"] == "signal_calculation_succeeded"


def test_multiple_failed_signals_are_one_daily_buy_task_with_full_diagnostics():
    step = {"state": "failed", "attempts": 2, "errors": ["600001.SH 缺分钟"],
            "pool_total": 10, "pool_usable": 9, "missing_count": 1}
    result = daily({"state": "failed", "signals": {
        "first": {"breadth": deepcopy(step), "stocks": deepcopy(step)},
        "second": {"stocks": {**step, "errors": ["600002.SH 昨收冲突"]}},
    }})
    events, _ = failure_events(result, RULES, {}, NOW)
    assert len(events) == 1
    assert events[0]["details"]["task"] == "buy"
    assert "600001.SH" in events[0]["message"] and "600002.SH" in events[0]["message"]
    assert set(events[0]["details"]["signals"]) == {"first", "second"}


def test_expired_task_never_executed_does_not_report_calculation_failure():
    task = {"state": "expired", "terminal": True, "attempts": 1,
            "phase": "observe", "errors": ["已超过任务窗口"],
            "retry_until": "2026-11-20T11:30:00+08:00",
            "history": [{"state": "expired", "errors": ["已超过任务窗口"]}]}
    assert failure_events(daily(task), RULES, {}, NOW)[0] == []


def test_successful_breadth_is_a_phase_not_a_completed_buy_task():
    result = daily({"state": "running", "terminal": False, "signals": {
        "late": {"breadth": {"state": "success", "terminal": True, "attempts": 1,
                              "passed": True, "errors": []},
                 "stocks": {"state": "waiting", "attempts": 0, "errors": []}},
    }})
    assert failure_events(result, RULES, {}, NOW)[0] == []
