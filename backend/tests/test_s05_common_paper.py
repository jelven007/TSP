"""S05 independent signals retain their saved exit and execution window."""
from datetime import datetime

import pytest
from test_paper_s02_execution import DAY, SYM, Repo

from app.market_time import CN_TZ
from app.strategy import paper, paper_minute
from app.strategy.late_session_strength import DEFAULT_PARAMS


@pytest.mark.parametrize("branch,start,end", [
    ("tail", "14:05:00", "14:16:00"), ("low_gap", "14:14:00", "14:25:00"),
    ("board", "14:40:00", "14:51:00"),
])
def test_common_entry_uses_own_saved_window_without_routed_policy(tmp_path, monkeypatch, branch, start, end):
    signal_at = datetime.fromisoformat(f"{DAY}T{start}")
    from datetime import timedelta
    now = (signal_at + timedelta(minutes=1)).replace(tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper.create_account(tmp_path, 1_000_000)
    entry = {"id": f"{branch}_entry_s05", "name": branch, "enabled": True, "kind": "entry",
             "template": "late_session_strength", "timeframe": "intraday", "conditions": [],
             "params": {**dict.fromkeys(DEFAULT_PARAMS), "time": start, "end_time": end,
                        "evaluation_delay_minutes": 1}}
    exit_point = {"id": f"{branch}_exit_s05", "template": "morning_liquidation",
                  "enabled": True, "params": {"time": "10:30:00", "fallback_time": "10:45:00"}}
    event = {"symbol": SYM, "price": 10., "entry_policy": {
        "strategy_id": "custom_mvxxcs05", "as_of": str(DAY),
        "entry_definition": entry, "exit_definition": exit_point,
        "signal_datetime": signal_at.isoformat(), "entry_evaluated_at": now.replace(tzinfo=None).isoformat(),
    }}
    rule = {"id": "test", "match_id": "custom_mvxxcs05", "side": "auto",
            "execution_mode": "s02_minute", "size_mode": "fixed_amount", "size_value": 10000}
    order, error = paper_minute.create_entry(tmp_path, rule, event, Repo(), now)
    assert error is None
    assert order["entry_policy"]["exit_definition"] == exit_point
    assert order["strategy_execution"]["end_at"].endswith(end)
    assert "execution_policy" not in order["strategy_execution"]
