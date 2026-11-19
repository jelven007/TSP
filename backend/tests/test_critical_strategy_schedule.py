"""Old system schedules retire once and cannot restore strategy monitoring."""
# ruff: noqa: F401,F811 -- reuse isolated PostgreSQL fixtures.
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from test_full_identity import env, full_cluster
from test_full_monitor import app_env
from test_full_runtime import owned, price_rule

from app.cell.context import AccessError
from app.market_time import CN_TZ
from app.multitenancy import compute_broker
from app.multitenancy.monitor_rules import Rules


def configure(owned):
    sid = "custom_test"
    owned.documents.put("strategy_sources", sid, {
        "code": "", "definition": {"meta": {"name": "S02"},
                                  "entry_signals": ["csgi_late_session_strength"]},
    })
    owned.documents.put("custom_signals", "late_session_strength", {
        "id": "late_session_strength", "enabled": True, "template": "late_session_strength",
        "params": {"time": "14:30:00", "evaluation_delay_minutes": .5},
    })
    owned.documents.put("critical_strategy_schedule", sid, {
        "enabled": True, "strategy_id": sid, "rule_id": "critical_test", "name": "S02",
    })
    return sid


def test_old_schedule_retires_once_without_touching_strategy_or_history(owned):
    from app.multitenancy.critical_schedule import check_completion, reconcile
    from app.services.strategy_monitor_tasks import set_monitoring
    sid = configure(owned)
    source = owned.documents.get("strategy_sources", sid)
    history = {"state": "succeeded", "buy_count": 3}
    owned.documents.put("critical_strategy_runs", sid + ":2026-11-12", history)
    reconcile(owned.worker, owned.documents)
    before = owned.documents.get("strategy_monitoring", sid)
    assert before["enabled"] is False
    assert owned.documents.get("critical_strategy_schedule", sid)["enabled"] is False
    assert Rules(owned.documents).load_all() == []
    reconcile(owned.worker, owned.documents)
    check_completion(owned.worker, owned.documents)
    assert owned.documents.get("strategy_monitoring", sid) == before
    assert owned.documents.get("strategy_sources", sid) == source
    assert owned.documents.get("critical_strategy_runs", sid + ":2026-11-12") == history
    set_monitoring(owned.documents, sid, "S02", True)
    enabled = Rules(owned.documents).load_all()
    reconcile(owned.worker, owned.documents)
    assert Rules(owned.documents).load_all() == enabled


@pytest.mark.parametrize("operation", ["delete", "disable", "redirect"])
def test_old_system_rule_is_an_ordinary_removable_rule(owned, operation):
    from app.strategy.monitor_rules import normalize
    rules = Rules(owned.documents)
    rules.save_one(normalize({
        "id": "critical_test", "name": "S02", "type": "strategy", "scope": "all",
        "strategy_id": "custom_test", "_system_task": "late_session_critical_v1",
    }))
    saved = rules.load_one("critical_test")
    assert not saved.get("_system_task")
    if operation == "delete":
        assert rules.delete_one(saved["id"])
    else:
        changes = {"enabled": False} if operation == "disable" else {"strategy_id": "other"}
        rules.save_one({**saved, **changes})
        assert all(rules.load_one(saved["id"])[k] == v for k, v in changes.items())


def test_expired_claim_cannot_migrate_monitor_state(owned):
    from app.multitenancy.critical_schedule import reconcile
    configure(owned)
    claim = {"tenant_id": owned.documents.context.tenant_id, "kind": "realtime",
             "owner": "expired", "revision": 1}
    with pytest.raises(AccessError, match="租约"):
        reconcile(owned.worker, owned.documents, claim)
    assert Rules(owned.documents).load_all() == []
    assert owned.documents.get("strategy_monitoring", "custom_test") is None


@pytest.mark.parametrize("hour,minute,second", [
    (14, 24, 59), (14, 25, 0), (14, 30, 30), (14, 55, 0), (14, 55, 1)])
def test_clock_no_longer_reserves_critical_compute_capacity(tmp_path, monkeypatch, hour, minute, second):
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(
        2026, 11, 19, hour, minute, second, tzinfo=CN_TZ))
    with TemporaryDirectory(dir="/tmp", prefix="tsp-c-") as short, compute_broker.ComputeBroker(
        Path(short) / "b.sock", "image", tmp_path, 1,
        host_memory_mb=16384, host_cpus=8,
    ) as broker:
        assert broker._resource_policy()["realtime_reserved_slots"] == 0


def test_native_api_can_delete_old_system_rule(app_env):
    from app.strategy.monitor_rules import normalize
    Rules(app_env["own"]).save_one(normalize({
        "id": "critical_api", "name": "S02", "type": "strategy",
        "strategy_id": "custom_test", "scope": "all",
        "_system_task": "late_session_critical_v1"}))
    assert app_env["client"].delete("/api/monitor-rules/critical_api").status_code == 200
    assert Rules(app_env["own"]).load_one("critical_api") is None
