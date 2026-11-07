"""Real PostgreSQL commits, retries and failure metadata."""
import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_runtime import owned as owned
from test_full_runtime import price_rule

from app.multitenancy import runtime
from app.multitenancy.alerts import Alerts
from app.multitenancy.monitor_rules import Rules


def test_compute_failure_records_retry_then_recovers_without_duplicate_alert(owned, monkeypatch):
    Rules(owned.documents).save_one(price_rule())
    tenant = owned.documents.context.tenant_id
    original = runtime.evaluate_snapshot
    monkeypatch.setattr(runtime, "evaluate_snapshot", lambda *_: (_ for _ in ()).throw(
        TimeoutError("compute timeout")))
    with pytest.raises(TimeoutError):
        owned.worker.evaluate(tenant, owned.snapshot())
    failed = owned.documents.get("strategy_execution_status", "default")
    assert failed["state"] == "retrying"
    assert failed["reason"] == "compute timeout"
    assert owned.documents.get("monitor_runtime", "default") is None
    monkeypatch.setattr(runtime, "evaluate_snapshot", original)
    assert owned.worker.evaluate(tenant, owned.snapshot()) == 1
    assert owned.documents.get("strategy_execution_status", "default")["state"] == "success"
    assert owned.worker.evaluate(tenant, owned.snapshot()) == 0
    assert len(Alerts(owned.documents).list_recent()) == 1


def test_configuration_change_cannot_publish_failure_for_old_input(owned, monkeypatch):
    Rules(owned.documents).save_one(price_rule())
    tenant = owned.documents.context.tenant_id
    def changed(*_):
        Rules(owned.documents).save_one(price_rule(conditions=[{"field": "close", "op": ">", "value": 12}]))
        raise TimeoutError("old compute")
    monkeypatch.setattr(runtime, "evaluate_snapshot", changed)
    with pytest.raises(TimeoutError):
        owned.worker.evaluate(tenant, owned.snapshot())
    assert owned.documents.get("strategy_execution_status", "default") is None


def test_stale_quotes_report_waiting_without_orders_or_runtime_progress(owned):
    Rules(owned.documents).save_one(price_rule())
    tenant = owned.documents.context.tenant_id
    snapshot = owned.snapshot(captured_at=owned.clock["now"] - 20)
    assert owned.worker.evaluate(tenant, snapshot) == 0
    assert owned.documents.get("strategy_execution_status", "default")["state"] == "waiting"
    assert owned.documents.get("monitor_runtime", "default") is None
