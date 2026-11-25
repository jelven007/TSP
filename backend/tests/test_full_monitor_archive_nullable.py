"""All three strategies commit real task receipts and alerts after old archives."""
# ruff: noqa: F401,F811 -- reusable isolated PostgreSQL fixtures.
from test_full_identity import env, full_cluster
from test_full_runtime import owned
from test_monitor_archive_nullable import SIDS, identity

from app.multitenancy.alerts import Alerts, append_in_transaction
from app.multitenancy.monitor_compute import failure_events
from app.services.strategy_monitor_state import CONTRACT
from app.services.strategy_results import StrategyResults, clear_memory


def test_three_strategies_archive_and_notify_in_one_postgres_transaction(owned):
    day = "2026-11-02"
    store = StrategyResults(owned.documents)
    for sid in SIDS:
        store.save(identity(sid), {"as_of": day, "rows": [], "total": 0,
                                  "monitor_tasks": None}, started_at=1)
    results = {sid: {"as_of": day, "rows": [], "total": 0, "monitor_contract": CONTRACT,
                     "monitor_tasks": {"buy": {"state": "rejected", "terminal": True,
                                               "errors": [], "count": 0},
                                       "sell": {"state": "running", "terminal": False,
                                                "errors": []}},
                     "section_statuses": {"buy": "rejected", "sell": "running"}}
               for sid in SIDS}
    rules = [{"id": sid, "name": sid, "strategy_id": sid} for sid in SIDS]
    from datetime import datetime

    from app.market_time import CN_TZ
    events, state = failure_events(results, rules, {}, datetime(2026, 11, 2, 14, 30, 30, tzinfo=CN_TZ))
    assert len(events) == 3
    with owned.documents.transaction() as unit:
        for sid, daily in results.items():
            saved = StrategyResults(unit).save(identity(sid), daily, started_at=2)
            unit.put("strategy_monitor_task_runs", f"{sid}:{day}", {
                "as_of": day, "tasks": saved["result"]["monitor_tasks"]})
        append_in_transaction(unit.connection, unit.context.tenant_id, events)
        unit.put("monitor_runtime", "default", {"signal_failures": state})
    clear_memory()
    for sid in SIDS:
        assert StrategyResults(owned.documents).read(identity(sid), day)["result"]["monitor_tasks"] == results[sid]["monitor_tasks"]
        assert owned.documents.get("strategy_monitor_task_runs", f"{sid}:{day}")["tasks"]["buy"]["state"] == "rejected"
        assert StrategyResults(owned.other).read(identity(sid), day) is None
    assert len(Alerts(owned.documents).list_recent(limit=20)) == 3
    assert failure_events(results, rules, state, datetime(2026, 11, 2, 14, 31, tzinfo=CN_TZ))[0] == []
