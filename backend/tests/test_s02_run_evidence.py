"""Semantic results preserve financial differences while attempt timing stays separate."""
from copy import deepcopy
from datetime import datetime, timedelta

from app.market_time import CN_TZ

SID = "custom_mvxxcp88"
NOW = datetime(2026, 11, 25, 14, 31, 2, tzinfo=CN_TZ)


def daily():
    return {"as_of": str(NOW.date()), "sections": {"buy": [{
        "symbol": "600001.SH", "close": 12.01, "atr_rank": 1, "score": 100.,
        "entry_evaluated_at": NOW.isoformat()}]},
        "entry_signal_hits": [{"symbol": "600001.SH", "signals": ["late"]}],
        "entry_data_quality": {"late": {"stage": "success", "progress": {
            "observed_at": NOW.isoformat(), "steps": {"change": {"state": "success",
                "processed": 1, "passed": 1, "elapsed_ms": 4}}}}},
        "monitor_tasks": {"buy": {"signals": {"late_session_strength": {
            "state": "success", "stocks": {"state": "success", "terminal": True,
                "passed": True, "rows": [], "attempts": 1}}}}}}


def payload():
    return {"strategy_task_windows": {SID: {"buy": [{
        "start": "14:30:00", "end": "14:55:00", "observe_breadth": True,
        "signal": {"id": "late_session_strength", "template": "late_session_strength",
                   "params": {"evaluation_delay_minutes": .5, "pool_up_min_pct": None}}}]}},
        "strategy_task_generations": {SID: 1}, "daily_identities": {SID: {
            "runtime_identity": "definition-one"}}, "strategy_task_results": {},
        "strategy_input_manifests": {SID: {"stocks": {"input_sha256": "a" * 64}}}}


def test_semantic_result_ignores_attempt_clock_but_preserves_price_rank_and_counts():
    from app.services.s02_run_evidence import semantic_result
    original = daily()
    repeated = deepcopy(original)
    repeated["sections"]["buy"][0]["entry_evaluated_at"] = "later"
    repeated["entry_data_quality"]["late"]["progress"]["steps"]["change"]["elapsed_ms"] = 99
    repeated["monitor_tasks"]["buy"]["signals"]["late_session_strength"]["stocks"]["attempts"] = 7
    expected = semantic_result(original, "stocks", "a" * 64)
    assert semantic_result(repeated, "stocks", "a" * 64) == expected
    for key, value in (("close", 12.02), ("atr_rank", 2), ("score", 99.)):
        changed = deepcopy(original)
        changed["sections"]["buy"][0][key] = value
        assert semantic_result(changed, "stocks", "a" * 64) != expected
    changed = deepcopy(original)
    changed["entry_data_quality"]["late"]["progress"]["steps"]["change"]["passed"] = 0
    assert semantic_result(changed, "stocks", "a" * 64) != expected


def test_watchdog_records_missing_trigger_input_delay_stuck_and_expiry():
    from app.services.s02_run_evidence import operational_state
    task = payload()
    result = operational_state(task, NOW, {})
    assert "missed_trigger" in {issue["kind"] for issue in result["issues"]}
    task["s02_input_status"] = {SID: {"stocks": {"state": "waiting_input"}}}
    assert "delayed_input" in {i["kind"] for i in operational_state(task, NOW, {})["issues"]}
    task["strategy_task_results"] = {SID: daily()}
    assert not any(i["phase"] == "stocks" for i in operational_state(task, NOW, {})["issues"])
    task["strategy_task_results"] = {}
    jobs = {"stocks": {"dispatched_at": (NOW - timedelta(seconds=61)).isoformat()}}
    assert "stuck_compute" in {i["kind"] for i in operational_state(task, NOW, jobs)["issues"]}
    assert "expired_unfinished" in {i["kind"] for i in operational_state(
        task, NOW.replace(hour=14, minute=55, second=1), {})["issues"]}
    assert operational_state(task, NOW.replace(hour=14, minute=30, second=19), {})["issues"] == []
