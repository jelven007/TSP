"""Canonical phases must accept older ordinary/selection result optional fields."""
import pytest

from app.services.strategy_monitor_state import CONTRACT
from app.services.strategy_results import ResultIdentity, StrategyResults, clear_memory

SIDS = ("custom_mvxxcs01", "custom_mvxxcp88", "custom_mvxxcs03")
DAY = "2026-11-23"


def identity(sid):
    return ResultIdentity(sid, sid, "logic", "{}", "{}", "null", "stock", "1m")


@pytest.mark.parametrize("sid", SIDS)
@pytest.mark.parametrize("old_tasks", [None, {}, {"sell": None}, {"sell": {"state": "success"}}])
@pytest.mark.parametrize("phase", ["buy", "sell"])
def test_canonical_phase_persists_after_nullable_archive(tmp_path, sid, old_tasks, phase):
    store = StrategyResults(tmp_path)
    key = identity(sid)
    old = {"as_of": DAY, "rows": [], "total": 0, "monitor_tasks": old_tasks,
           "section_statuses": {"buy": "waiting", "sell": "success"},
           "exit_signal_hits": [{"symbol": "600001.SH", "reason": "stop_time"}]}
    store.save(key, old, started_at=1)
    current = {"as_of": DAY, "rows": [], "total": 0,
               "monitor_contract": CONTRACT,
               "monitor_tasks": {"buy": {"state": "success" if phase == "buy" else "waiting"},
                                 "sell": {"state": "running"}},
               "section_statuses": {"buy": "success" if phase == "buy" else "waiting",
                                    "sell": "running"}}
    saved = store.save(key, current, started_at=2)
    assert saved["result"]["monitor_tasks"] == current["monitor_tasks"]
    assert saved["result"]["exit_signal_hits"] == old["exit_signal_hits"]
    assert saved["result"]["section_statuses"]["sell"] == (
        "success" if old_tasks and old_tasks.get("sell") else "running")
    clear_memory()
    assert StrategyResults(tmp_path).read(key, DAY) == saved


@pytest.mark.parametrize("sid", SIDS)
def test_null_current_sell_is_an_unfinished_phase(tmp_path, sid):
    store = StrategyResults(tmp_path)
    key = identity(sid)
    daily = {"as_of": DAY, "rows": [], "total": 0, "monitor_contract": CONTRACT,
             "monitor_tasks": {"sell": None},
             "section_statuses": {"buy": "waiting", "sell": "waiting"}}
    assert store.save(key, daily, started_at=1)["result"]["monitor_tasks"] == {"sell": None}


@pytest.mark.parametrize("sid", SIDS)
@pytest.mark.parametrize("phase", ["breadth", "stocks"])
def test_phase_can_resume_serialized_ordinary_result(monkeypatch, sid, phase):
    from dataclasses import asdict
    from datetime import date, datetime
    from types import SimpleNamespace

    import polars as pl

    from app.market_time import CN_TZ
    from app.multitenancy import monitor_compute
    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyResult
    from app.strategy.late_session_strength import Evaluation

    now = datetime(2026, 11, 23, 14, 30, 15, tzinfo=CN_TZ)
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: now)
    monkeypatch.setattr(ScreenerService, "_load_enriched_history", lambda *_: pl.DataFrame())
    monkeypatch.setattr("app.strategy.late_session_strength.previous_closes", lambda *_a, **_k: {})
    monkeypatch.setattr("app.strategy.late_session_strength.evaluate", lambda *_a, **_k:
                        Evaluation(stage="breadth_rejected", pool_total=1))
    sig = {"id": "late", "params": {"time": "14:30:00", "evaluation_delay_minutes": .5}}
    action = {"kind": "buy", "signal_id": "late", "phase": phase,
              "scheduled_at": now.isoformat(), "retry_until": "2026-11-23T14:30:30+08:00"}
    previous = asdict(StrategyResult(as_of=date(2026, 11, 23), strategy_id=sid))
    previous["as_of"] = DAY
    payload = {"state": {}, "rules": [{"strategy_id": sid}], "strategy_task_ids": [sid],
               "strategy_task_plans": {sid: {"actions": [action]}},
               "strategy_task_windows": {sid: {"buy": [{"signal": sig, "start": "14:30:00", "end": "14:55:00"}]}},
               "strategy_task_receipts": {}, "strategy_task_results": {sid: previous},
               "daily_identities": {sid: {"runtime_identity": "logic",
                                         "asset_type": "stock", "timeframe": "1m"}},
               "daily_pool_snapshots": {sid: {"symbols": ["600001.SH"]}},
               "late_task_scopes": {sid: "scope"}, "selection_snapshots": {}, "sell_rosters": {}}
    repo = SimpleNamespace(get_minute_batch=lambda *_a, **_k: pl.DataFrame())

    def fail(*_a, **_k):
        raise RuntimeError("sandbox unavailable")

    daily = monitor_compute.evaluate_strategy_phases(payload, repo, SimpleNamespace(execute=fail))["results"][sid]
    if phase == "breadth":
        assert daily["monitor_tasks"]["buy"]["state"] == "rejected"
        assert isinstance(daily["entry_data_quality"], dict)
    else:
        assert daily["monitor_tasks"]["buy"]["state"] == "failed"
        assert daily["section_errors"]["buy"] == "sandbox unavailable"
    assert isinstance(daily["sections"], dict)
    assert daily["section_statuses"]["buy"] in ("rejected", "failed")
