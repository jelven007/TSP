"""A previous session's frozen phase cannot become today's completed phase."""
from copy import deepcopy
from datetime import datetime

import pytest

from app.market_time import CN_TZ
from app.multitenancy.monitor_phase_processes import merge_daily
from app.services.strategy_monitor_state import plan

DAY = "2026-11-24"
OLD_DAY = "2026-11-23"
SIGNAL = "late_session_strength"


def windows():
    return {"buy": [{
        "start": "14:30:00", "end": "14:55:00",
        "phase_policy": "independent_v1", "observe_breadth": True,
        "signal": {"id": SIGNAL, "template": "late_session_strength",
                   "params": {"pool_up_min_pct": None, "evaluation_delay_minutes": .5}},
    }], "sell": []}


def daily(day, *, breadth=None):
    value = {"state": "waiting", "terminal": False, "phase_policy": "independent_v1"}
    if breadth is not None:
        value["breadth"] = deepcopy(breadth)
    return {"as_of": day, "monitor_tasks": {
        "buy": {"state": "waiting", "terminal": False, "signals": {SIGNAL: value}}}}


OLD_BREADTH = {
    "state": "success", "terminal": True, "observation_only": True,
    "observed_at": OLD_DAY + "T14:30:15", "pool_up_pct": 72.20216606498195,
}


def test_sell_phase_cannot_carry_yesterday_breadth_into_today_or_skip_1430():
    previous = daily(OLD_DAY, breadth=OLD_BREADTH)
    current = daily(DAY)
    merged = merge_daily(previous, current, "stocks", [{"kind": "sell", "phase": "observe"}])
    now = datetime(2026, 11, 24, 14, 30, 15, tzinfo=CN_TZ)
    actions = plan(windows(), merged["monitor_tasks"], now, trading=True)["actions"]
    assert [action["phase"] for action in actions] == ["breadth"]
    assert "breadth" not in merged["monitor_tasks"]["buy"]["signals"][SIGNAL]
    assert previous == daily(OLD_DAY, breadth=OLD_BREADTH)


def test_breadth_phase_cannot_keep_yesterday_as_the_daily_result_date():
    previous = daily(OLD_DAY, breadth=OLD_BREADTH)
    today_breadth = {**OLD_BREADTH, "observed_at": DAY + "T14:30:15", "pool_up_pct": 40.}
    current = daily(DAY, breadth=today_breadth)
    merged = merge_daily(previous, current, "breadth", [
        {"kind": "buy", "phase": "breadth", "signal_id": SIGNAL}])
    assert merged["as_of"] == DAY
    assert merged["monitor_tasks"]["buy"]["signals"][SIGNAL]["breadth"] == today_breadth
    assert previous == daily(OLD_DAY, breadth=OLD_BREADTH)


@pytest.mark.parametrize("phase", ["stocks", "breadth"])
def test_same_day_phase_ownership_remains_intact(phase):
    breadth = {**OLD_BREADTH, "observed_at": DAY + "T14:30:15"}
    previous = daily(DAY, breadth=breadth)
    current = daily(DAY, breadth=breadth if phase == "breadth" else None)
    actions = [{"kind": "buy", "phase": phase, "signal_id": SIGNAL}]
    merged = merge_daily(previous, current, phase, actions)
    assert merged["as_of"] == DAY
    assert merged["monitor_tasks"]["buy"]["signals"][SIGNAL]["breadth"] == breadth
