"""Standalone scheduling executes and archives exactly the same dated phases."""
# ruff: noqa: F401,F811 -- share real minute strategy fixture.
import time
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest
from test_minute_signal_sections import scan

from app.market_time import CN_TZ
from app.services.screener import ScreenerService


@pytest.mark.parametrize("change", ["cancel", "configuration", "deadline"])
def test_local_commit_discards_cancelled_changed_or_late_output(scan, tmp_path, monkeypatch, change):
    from app.services import local_strategy_monitor
    from app.services.alert_store import list_recent
    from app.services.strategy_monitor_state import CONTRACT
    from app.services.strategy_monitor_tasks import set_monitoring, task_receipt
    from app.services.strategy_results import StrategyResults
    from app.strategy.config import save_override
    engine, context, _ = scan
    engine._user_data_dir = lambda: tmp_path
    now = {"value": datetime(2026, 11, 2, 14, 30, 30, tzinfo=CN_TZ)}
    monkeypatch.setattr(local_strategy_monitor, "cn_now", lambda: now["value"])
    set_monitoring(tmp_path, "custom_test", "Test", True)
    monkeypatch.setattr(local_strategy_monitor, "task_windows", lambda *_: {
        "buy": [{"start": "14:30:00", "confirm": "14:30:30", "end": "14:55:00",
                 "signal": {"id": "trigger"}}], "sell": []})
    row = {"symbol": "600001.SH", "score": 1.}
    def evaluate(payload, *_):
        if change == "cancel":
            set_monitoring(tmp_path, "custom_test", "Test", False)
        elif change == "configuration":
            save_override(tmp_path, "custom_test", {"params": {"window": 99}})
        else:
            now["value"] = now["value"].replace(minute=55, second=1)
        daily = {**payload["daily_identities"]["custom_test"], "as_of": str(context.as_of),
            "monitor_contract": CONTRACT, "rows": [row], "total": 1,
            "sections": {"buy": [row], "sell": [], "select": []},
            "monitor_executed_signals": {"buy": ["trigger"]},
            "monitor_tasks": {"buy": {"state": "success", "terminal": True, "published": True,
                "signals": {"trigger": {"state": "success", "terminal": True,
                    "stocks": {"state": "success", "terminal": True, "rows": [row]}}}}}}
        return {"results": {"custom_test": daily}, "state": {}, "events": [{
            "strategy_id": "custom_test", "type": "buy_signal", "symbol": row["symbol"],
            "ts": int(time.time() * 1000)}]}
    monkeypatch.setattr(local_strategy_monitor, "evaluate_strategy_phases", evaluate)
    repo = SimpleNamespace(get_instruments_asset=lambda *_: context.current)
    changed = local_strategy_monitor.run(engine, ScreenerService(repo), tmp_path)
    if change == "deadline":
        assert changed == ["custom_test"]
        assert task_receipt(tmp_path, "custom_test", context.as_of)["tasks"]["buy"]["state"] == "expired"
    else:
        assert changed == []
        assert StrategyResults(tmp_path).day_view("custom_test", str(context.as_of)) is None
    assert not [event for event in list_recent(tmp_path) if event.get("type") == "buy_signal"]


@pytest.mark.parametrize("lost_receipt", [False, True])
def test_local_start_plus_fifteen_confirmation_and_restart_are_fixed(scan, tmp_path, monkeypatch, lost_receipt):
    from app.services import local_strategy_monitor
    from app.services.daily_selections import DailySelections
    from app.services.strategy_monitor_tasks import set_monitoring, task_receipt
    from app.services.strategy_results import capture_identity
    from app.strategy import custom_signals, overnight_runtime
    engine, context, _ = scan
    engine._user_data_dir = lambda: tmp_path
    now = {"value": datetime(2026, 11, 2, 14, 30, 15, tzinfo=CN_TZ)}
    for module in ("app.market_time", "app.multitenancy.monitor_compute", "app.services.local_strategy_monitor"):
        monkeypatch.setattr(module + ".cn_now", lambda: now["value"])
    custom_signals.save_one(tmp_path, {
        "id": "trigger", "name": "尾盘", "kind": "entry", "timeframe": "intraday",
        "template": "late_session_strength", "enabled": True, "conditions": [],
        "params": {"time": "14:30:00", "end_time": "14:55:00",
                   "evaluation_delay_minutes": .5, "pool_up_min_pct": 60}})
    set_monitoring(tmp_path, "custom_test", "Test", True)
    identity = capture_identity(engine, tmp_path, "custom_test", timeframe="1m")
    DailySelections(tmp_path).save(identity, {
        "as_of": str(context.as_of), "source_date": "2026-10-30",
        "rows": context.current.filter(pl.col("symbol").str.starts_with("600")).to_dicts()}, started_at=1)
    repo = SimpleNamespace(get_instruments_asset=lambda *_: context.current,
                           get_minute_batch=lambda *_a, **_k: context.history)
    monkeypatch.setattr(ScreenerService, "_load_enriched_history", lambda *_: context.daily_history)
    def load(*_a, **_k):
        from dataclasses import replace
        return replace(context, observed_at=now["value"], completed_through=now["value"], realtime=True)
    monkeypatch.setattr(overnight_runtime, "monitor_context", load)
    service = ScreenerService(repo)
    if lost_receipt:
        original = local_strategy_monitor.task_receipt
        def fail_receipt(storage, sid, day, value=None):
            if sid == "custom_test" and value is not None:
                raise OSError("回执写入失败")
            return original(storage, sid, day, value)
        monkeypatch.setattr(local_strategy_monitor, "task_receipt", fail_receipt)
        with pytest.raises(OSError, match="回执写入失败"):
            local_strategy_monitor.run(engine, service, tmp_path)
        monkeypatch.setattr(local_strategy_monitor, "task_receipt", original)
        assert local_strategy_monitor.run(engine, service, tmp_path) == []
    else:
        assert local_strategy_monitor.run(engine, service, tmp_path) == ["custom_test"]
    breadth = task_receipt(tmp_path, "custom_test", context.as_of)
    if not lost_receipt:
        assert breadth["tasks"]["buy"]["signals"]["trigger"]["breadth"]["state"] == "success"
    now["value"] = now["value"].replace(second=30)
    assert local_strategy_monitor.run(engine, service, tmp_path) == ["custom_test"]
    done = deepcopy(task_receipt(tmp_path, "custom_test", context.as_of))
    assert done["tasks"]["buy"]["terminal"]
    now["value"] = now["value"].replace(minute=40)
    assert local_strategy_monitor.run(engine, service, tmp_path) == []
    assert task_receipt(tmp_path, "custom_test", context.as_of) == done
