"""Display-only breadth has its own schedule and cannot change buy outcomes."""
from copy import deepcopy
from datetime import datetime

import pytest
from test_s02_independent_entry import independent_windows

from app.market_time import CN_TZ
from app.services.strategy_monitor_state import join_buy, plan


def observation_windows():
    windows = independent_windows()
    windows["buy"][0]["signal"]["params"]["pool_up_min_pct"] = None
    windows["buy"][0]["observe_breadth"] = True
    return windows


NOW = datetime(2026, 11, 23, 14, 30, 30, tzinfo=CN_TZ)
STOCKS = {"state": "success", "terminal": True, "passed": True, "errors": [],
          "completed_at": NOW.isoformat(), "rows": [{"symbol": "a", "score": 1}],
          "hits": [{"symbol": "a"}]}


@pytest.mark.parametrize("second,phases", [(14, []), (15, ["breadth"]), (30, ["breadth", "stocks"])])
def test_observation_has_fifteen_second_clock_without_a_threshold(second, phases):
    actions = plan(observation_windows(), {}, NOW.replace(second=second), trading=True)["actions"]
    assert [a["phase"] for a in actions] == phases
    for action in actions:
        assert action.get("observation_only", False) == (action["phase"] == "breadth")


@pytest.mark.parametrize("state", ["success", "rejected"])
def test_finished_buy_does_not_stop_unfinished_observation(state):
    tasks = {"buy": {"state": state, "terminal": True, "signals": {"late": {
        "state": state, "terminal": True, "stocks": STOCKS,
        "breadth": {"state": "failed", "terminal": False, "errors": ["missing"]},
    }}}}
    actions = plan(observation_windows(), tasks, NOW.replace(minute=31), trading=True)["actions"]
    assert [a["phase"] for a in actions] == ["breadth"]
    assert actions[0]["observation_only"] is True
    expired = plan(observation_windows(), tasks, NOW.replace(minute=55), trading=True)["actions"]
    assert [a["phase"] for a in expired] == ["expire"]
    assert expired[0]["expired_phase"] == "breadth" and expired[0]["observation_only"]


@pytest.mark.parametrize("breadth_state", ["waiting", "failed", "rejected", "success", "expired", "cancelled"])
@pytest.mark.parametrize("first", ["breadth", "stocks"])
def test_only_stocks_gate_buy_when_threshold_is_empty(breadth_state, first):
    breadth = {"state": breadth_state, "terminal": breadth_state not in ("waiting", "failed"),
               "passed": breadth_state == "success", "errors": ["missing"] if breadth_state == "failed" else []}
    phases = {"breadth": breadth, "stocks": STOCKS}
    task, rows, hits = join_buy({"signals": {"late": {first: phases[first]}}}, observation_windows()["buy"], NOW)
    assert bool(rows) == (first == "stocks")
    task["signals"]["late"].update(phases)
    task, rows, hits = join_buy(task, observation_windows()["buy"], NOW)
    assert task["state"] == "success" and task["published"] and not task["errors"]
    assert task["signals"]["late"]["breadth_required"] is False
    assert rows[0]["symbol"] == hits[0]["symbol"] == "a"
    assert join_buy(task, observation_windows()["buy"], NOW.replace(hour=15)) == (task, rows, hits)


def test_observation_evaluates_real_breadth_and_freezes_it_without_inventing_a_gate():
    from test_late_session_optional_params import AT, DAY, signal
    from test_late_session_strength import POOL, bars

    from app.strategy.late_session_strength import evaluate
    state = {}
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=None)
    result = evaluate(bars(), sig, day=DAY, pool_symbols=POOL, prev_close={s: 10. for s in POOL},
                      completed_through=AT.replace(second=15), task_state=state, independent_phase="breadth")
    assert result.pool_up_pct == 60 and not result.diagnostics
    assert result.progress["params"]["pool_up_min_pct"] is None
    assert result.progress["steps"]["breadth"]["state"] == "success"
    assert result.progress["steps"]["breadth"]["reason"] == "仅展示, 不参与买入筛选"
    assert evaluate(None, sig, day=DAY, pool_symbols=POOL, prev_close={},
                    completed_through=AT.replace(second=30), task_state=state,
                    independent_phase="breadth") == result


def test_no_valid_reference_is_unknown_observation_and_stocks_still_pass():
    from test_late_session_optional_params import AT, DAY, bars, signal

    from app.strategy.late_session_strength import evaluate
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=None)
    result = evaluate(bars(), sig, day=DAY, pool_symbols=["a"], prev_close={},
                      completed_through=AT.replace(second=15), independent_phase="breadth")
    assert result.diagnostics and result.pool_up_pct is None
    assert result.progress["steps"]["breadth"]["state"] == "failed"


def test_legacy_definition_without_observation_metadata_still_skips_breadth():
    windows = observation_windows()
    del windows["buy"][0]["observe_breadth"]
    assert [a["phase"] for a in plan(windows, {}, NOW, trading=True)["actions"]] == ["stocks"]


def test_late_failed_observation_retains_published_buy_and_does_not_emit_failure():
    from app.multitenancy.monitor_compute import failed_phase_results, failure_events
    from app.services.strategy_monitor_tasks import apply_independent_buy, fence_tasks
    daily = {"as_of": str(NOW.date()), "monitor_tasks": {"buy": {"signals": {"late": {
        "stocks": deepcopy(STOCKS)}}}}}
    apply_independent_buy(daily, observation_windows(), NOW)
    before_rows = deepcopy(daily["rows"])
    rules = [{"id": "monitor", "strategy_id": "s", "name": "s02"}]
    _, alert_state = failure_events({"s": daily}, rules, {}, NOW)
    later = NOW.replace(minute=56)
    actions = plan(observation_windows(), daily["monitor_tasks"], later, trading=True)["actions"]
    payload = {"strategy_task_ids": ["s"], "strategy_task_windows": {"s": observation_windows()},
               "strategy_task_receipts": {"s": {"tasks": daily["monitor_tasks"]}},
               "strategy_task_results": {"s": daily}, "daily_identities": {"s": {}},
               "strategy_task_plans": {"s": {"actions": actions}}}
    failed = failed_phase_results(payload, "observation unavailable", later)["s"]
    assert failed["monitor_tasks"]["buy"]["state"] == "success"
    assert failed["rows"] == before_rows
    assert failed["monitor_tasks"]["buy"]["signals"]["late"]["breadth"]["state"] == "expired"
    assert not failed["section_errors"].get("buy")
    assert failure_events({"s": failed}, rules, alert_state, later)[0] == []
    assert fence_tasks(failed, observation_windows(), later) == set()
    assert failed["rows"] == before_rows
