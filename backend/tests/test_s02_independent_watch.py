"""An API observer detects S02 without invoking the stopped Worker or Compute."""
# ruff: noqa: F401,F811 -- isolated PostgreSQL fixtures.
from datetime import datetime

from test_full_identity import env, full_cluster
from test_full_runtime import owned

from app.market_time import CN_TZ
from app.services.s02_run_evidence import SID


def test_independent_observer_records_outage_without_worker_or_notifications(owned, monkeypatch):
    from app.multitenancy.s02_watch import S02Watch
    from app.services.strategy_monitor_tasks import set_monitoring

    now = datetime(2026, 11, 25, 14, 30, 36, tzinfo=CN_TZ)
    owned.clock["now"] = now.timestamp()
    owned.documents.put("strategy_sources", SID, {
        "source": "custom", "code": "unchanged",
        "definition": {"strategy_id": SID}})
    set_monitoring(owned.documents, SID, "s02", True)
    monkeypatch.setattr("app.market_time.published_trading_day", lambda _: True)
    windows = {"buy": [{"start": "14:30:00", "end": "14:55:00",
        "observe_breadth": True, "signal": {"id": "entry", "params": {
            "evaluation_delay_minutes": .5, "pool_up_min_pct": None}}}], "sell": []}
    monkeypatch.setattr(S02Watch, "_windows", staticmethod(lambda *_: (windows, "definition")))
    watcher = S02Watch(owned.env["db"], owned.env["identity"])
    with owned.documents.transaction() as unit:
        watcher.observe(unit, now, worker_ready=False)
    state = owned.documents.get("strategy_operational_state", f"{SID}:{now.date()}")
    assert state["observer"] == "api"
    assert {i["kind"] for i in state["issues"]} >= {"worker_unavailable", "missed_trigger"}
    count = len(owned.documents.records("strategy_operational_records"))
    with owned.documents.transaction() as unit:
        watcher.observe(unit, now, worker_ready=False)
    assert len(owned.documents.records("strategy_operational_records")) == count
    with owned.documents.transaction(write=False) as unit:
        assert unit.connection.execute(
            "SELECT count(*) AS n FROM business.notification_outbox").fetchone()["n"] == 0
    assert owned.other.get("strategy_operational_state", f"{SID}:{now.date()}") is None
    watcher.close()


def test_watch_tick_reads_saved_tenant_definition_without_worker(owned, tmp_path, monkeypatch):
    from dataclasses import replace

    from test_minute_signal_sections import CODE

    from app.multitenancy.s02_watch import S02Watch
    from app.multitenancy.strategies import definition_payload
    from app.services.strategy_monitor_tasks import set_monitoring
    from app.strategy.engine import StrategyEngine

    (tmp_path / "custom.py").write_text(CODE)
    strategy = StrategyEngine([tmp_path]).get("custom_test")
    strategy = replace(strategy, entry_signals=["csgi_late_session_strength"],
        meta={**strategy.meta, "id": SID, "name": "s02",
        "entry_phase_policy": "independent_v1", "entry_breadth_observation": True})
    owned.documents.put("strategy_sources", SID, {
        "source": "custom", "code": CODE, "definition": definition_payload(strategy)})
    owned.documents.put("custom_signals", "late_session_strength", {
        "id": "late_session_strength", "kind": "entry", "timeframe": "intraday",
        "enabled": True, "template": "late_session_strength", "params": {
            "time": "14:30:00", "end_time": "14:55:00", "evaluation_delay_minutes": .5}})
    set_monitoring(owned.documents, SID, "s02", True)
    now = datetime(2026, 11, 25, 14, 30, 36, tzinfo=CN_TZ)
    owned.clock["now"] = now.timestamp()
    monkeypatch.setattr("app.market_time.published_trading_day", lambda _: True)
    loaded = []

    def unrelated_strategy(path):
        loaded.append(path.name)
        raise AssertionError("S02 observation must not load unrelated builtin code under the tenant lock")

    monkeypatch.setattr(StrategyEngine, "_load_file", staticmethod(unrelated_strategy))
    watcher = S02Watch(owned.env["db"], owned.env["identity"])
    watcher.tick()
    assert loaded == [], "S02 observation loaded unrelated builtin code while holding the tenant lock"
    state = owned.documents.get("strategy_operational_state", f"{SID}:{now.date()}")
    assert state and state["runtime_identity"]
    assert any(i["kind"] == "missed_trigger" for i in state["issues"])
    assert owned.documents.get("strategy_monitor_task_runs", f"{SID}:{now.date()}") is None
    watcher.close()
