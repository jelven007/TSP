"""Paper orders parse empty exit clocks without changing the frozen signal."""
from datetime import datetime, time, timedelta

import pytest
from test_paper_s02_execution import DAY, advance, bars, entry
from test_paper_s02_execution import setup as paper_setup

from app.strategy import paper

setup = paper_setup


@pytest.mark.parametrize("params,at,end", [
    ({"time": "10:30:00"}, "10:30:00", "10:30:00"),
    ({"fallback_time": "10:45:00"}, "10:45:00", "10:45:00"),
    ({"rise_pct": 6}, "09:35:00", "11:30:00"),
])
def test_optional_clock_order_end_is_separate_from_enabled_signal(setup, params, at, end):
    storage, _clock, _rule, repo, event = setup
    saved = dict.fromkeys(("start_time", "rise_pct", "fall_pct", "decline_3m_pct",
                          "volume_ratio_min", "time", "fallback_time"))
    saved.update(params)
    event["entry_policy"]["exit_definition"]["params"] = saved
    entry(setup)
    repo.frame = bars(datetime.combine(DAY, time(14, 31)))
    advance(setup, datetime.combine(DAY, time(14, 35)))
    next_day = DAY + timedelta(days=1)
    repo.frame = bars(datetime.combine(next_day, time(9, 31)), close=10.6)
    advance(setup, datetime.fromisoformat(f"{next_day}T{at}"))
    sell, = [order for order in paper.load_orders(storage) if order["side"] == "sell"]
    assert sell["strategy_execution"]["end_at"].endswith(end)
    assert event["entry_policy"]["exit_definition"]["params"] == saved
    assert sell["strategy_execution"]["exit_reason"] == (
        "stop_time" if "time" in params else "fallback" if "fallback_time" in params else "rise")
