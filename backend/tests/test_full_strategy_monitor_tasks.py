"""Strategy cancellation fences old work and disables legacy recovery atomically."""
# ruff: noqa: F401,F811 -- isolated PostgreSQL fixture reuse.
from types import SimpleNamespace

import pytest
from test_critical_strategy_schedule import configure
from test_full_daily_stock_pools import upgrade
from test_full_identity import env, full_cluster
from test_full_runtime import owned

from app.api import monitor_rules as api
from app.multitenancy.critical_schedule import reconcile
from app.multitenancy.monitor_rules import Rules
from app.multitenancy.runtime import fingerprint
from app.strategy.monitor_rules import normalize


def test_cancel_custom_disables_all_rules_recovery_and_changes_commit_fingerprint(owned, monkeypatch):
    sid = configure(owned)
    reconcile(owned.worker, owned.documents)
    Rules(owned.documents).save_one(normalize({
        "id": "ordinary_duplicate", "name": "S02", "type": "strategy", "scope": "all",
        "strategy_id": sid,
    }))
    monkeypatch.setattr(api, "_data_dir", lambda _: owned.documents)
    from app.api import strategy as strategy_api
    monkeypatch.setattr(strategy_api, "_get_engine", lambda _: SimpleNamespace(
        get=lambda _: SimpleNamespace(source="custom", meta={"name": "S02"})))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=owned.repo)))
    with owned.documents.transaction(write=False) as unit:
        before = fingerprint(unit.connection, include_state=False)
    cancelled = api.set_strategy_monitoring(sid, api.StrategyMonitoringModel(enabled=False), request)
    assert cancelled["enabled"] is False
    assert not any(rule["enabled"] for rule in Rules(owned.documents).load_all())
    assert owned.documents.get("critical_strategy_schedule", sid)["enabled"] is False
    reconcile(owned.worker, owned.documents)
    assert not any(rule["enabled"] for rule in Rules(owned.documents).load_all())
    assert owned.other.get("strategy_monitoring", sid) is None
    with owned.documents.transaction(write=False) as unit:
        assert fingerprint(unit.connection, include_state=False) != before
    enabled = api.set_strategy_monitoring(sid, api.StrategyMonitoringModel(enabled=True), request)
    assert enabled["enabled"]
    assert sum(rule["enabled"] for rule in Rules(owned.documents).load_all()) == 1
    with owned.documents.transaction(write=False) as unit:
        assert fingerprint(unit.connection, include_state=False) != before


def test_failed_switch_rolls_back_both_lifecycle_and_rules(owned, monkeypatch):
    from app.services.strategy_monitor_tasks import set_monitoring
    original = Rules.save_one
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("rollback")
    monkeypatch.setattr(Rules, "save_one", fail)
    with pytest.raises(RuntimeError, match="rollback"):
        set_monitoring(owned.documents, "custom_test", "Test", True)
    assert owned.documents.get("strategy_monitoring", "custom_test") is None
    assert Rules(owned.documents).load_all() == []


def test_cancelled_strategy_cannot_run_via_reenabled_legacy_rule(owned, tmp_path):
    from test_full_daily_selections import source

    from app.services.strategy_monitor_tasks import set_monitoring
    source(owned.documents, tmp_path)
    rule = Rules(owned.documents).load_all()[0]
    set_monitoring(owned.documents, "custom_test", "Test", False)
    Rules(owned.documents).save_one({**rule, "enabled": True})
    _, _, payload, _ = owned.worker.prepare(
        owned.documents.context.tenant_id, owned.snapshot(), None)
    assert not payload["rules"]
    assert not payload["strategy_task_ids"]


def test_deleted_buy_receipt_removes_forward_roster_on_postgres(owned):
    from test_strategy_sell_rosters import bought, identity

    from app.services.strategy_monitor_tasks import sell_roster
    from app.services.strategy_results import StrategyResults
    store = StrategyResults(owned.documents)
    saved = store.save(identity(), bought([{"symbol": "600001.SH"}]), started_at=1)
    store.delete("custom_test", saved["record_id"])
    assert sell_roster(owned.documents, "custom_test", "2026-11-02") is None
    assert store.day_view("custom_test", "2026-11-02") is None


@pytest.mark.parametrize("rows", [[], [{"symbol": "600001.SH", "close": 10.}]])
def test_next_sell_roster_and_rollback_are_private_on_postgres(owned, rows):
    from test_strategy_sell_rosters import bought, identity

    from app.services.strategy_monitor_tasks import sell_roster
    from app.services.strategy_results import StrategyResults
    with pytest.raises(RuntimeError), owned.documents.transaction() as unit:
        StrategyResults(unit).save(identity(), bought(rows), started_at=1)
        assert sell_roster(unit, "custom_test", "2026-11-02")["rows"] == [
            {**r, "entry_date": "2026-10-30",
             "entry_record_id": identity().record_id("2026-10-30")} for r in rows]
        raise RuntimeError("rollback")
    assert sell_roster(owned.documents, "custom_test", "2026-11-02") is None
    StrategyResults(owned.documents).save(identity(), bought(rows), started_at=2)
    assert sell_roster(owned.documents, "custom_test", "2026-11-02") is not None
    assert sell_roster(owned.other, "custom_test", "2026-11-02") is None


def test_active_sell_task_runs_without_fresh_quote_and_archives_independent_result(owned, tmp_path):
    import gzip
    import json
    from datetime import datetime, time

    from test_full_daily_selections import source
    from test_strategy_sell_rosters import bought, identity
    from test_strategy_sell_tasks import SIGNAL

    from app.market_time import CN_TZ, cn_today
    from app.services.strategy_results import StrategyResults
    from app.strategy.basic_filter import previous_session
    source(owned.documents, tmp_path)
    owned.documents.put("custom_signals", "morning", SIGNAL)
    owned.documents.put("strategy_overrides", "custom_test", {"exit_signals": ["csgi_morning"]})
    prior = str(previous_session(cn_today()))
    StrategyResults(owned.documents).save(identity(), {
        **bought([{"symbol": "600001.SH"}]), "as_of": prior,
    }, started_at=1)
    owned.clock["now"] = datetime.combine(cn_today(), time(10, 50), CN_TZ).timestamp()
    calls = []
    def execute(payload, **kwargs):
        calls.append(payload)
        assert payload["sell_rosters"]["custom_test"]["source_date"] == prior
        daily = {"as_of": str(cn_today()), "section_mode": "signals", "rows": [], "total": 0,
                 "entry_filter_error": "等待入场", "section_errors": {"buy": "等待入场"},
                 "section_statuses": {"buy": "waiting", "sell": "success"},
                 "sections": {"sell": payload["sell_rosters"]["custom_test"]["rows"], "buy": [], "select": []},
                 "exit_signal_hits": [{"symbol": "600001.SH", "signal_roster": True, "reason": "stop_time"}],
                 "monitor_tasks": {"sell": {"state": "success", "count": 1}}}
        return {"events": [], "execution_events": [], "state": {},
                "results": {"custom_test": daily}}
    owned.worker.executor = SimpleNamespace(execute=execute)
    owned.worker.evaluate(owned.documents.context.tenant_id,
                          owned.snapshot(status={"is_trading_hours": False}))
    assert len(calls) == 1
    view = StrategyResults(owned.documents).day_view("custom_test", str(cn_today()))
    result = json.loads(gzip.decompress(view[1]))["result"]
    assert result["exit_signal_hits"][0]["reason"] == "stop_time"
    assert result["section_statuses"]["buy"] == "waiting"
