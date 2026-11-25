"""Real PostgreSQL verifies early commit and rollback redelivery of separate phases."""
# ruff: noqa: F401,F811 -- isolated database fixtures are imported deliberately.
import threading
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_full_identity import env, full_cluster
from test_full_runtime import owned
from test_monitor_phase_processes import NOW, SID, SIGNAL, payload, reply

from app.multitenancy.alerts import Alerts
from app.multitenancy.runtime import fingerprint
from app.services.strategy_monitor_tasks import set_monitoring, task_receipt
from app.services.strategy_results import ResultIdentity, StrategyResults


def configure(owned, monkeypatch):
    sid = SID
    saved = set_monitoring(owned.documents, sid, "s02", True)
    owned.clock["now"] = NOW.timestamp()
    original = owned.worker.prepare
    record = ResultIdentity(sid, "s02", "same-definition", "{}", "{}", "null", "stock", "1m")

    def prepare(*args):
        account, context, current, observed = original(*args)
        task = deepcopy(payload())
        task["tenant_id"] = context.tenant_id
        task["state"] = current["state"]
        task["rules"] = current["rules"]
        task["daily_identities"][sid] = asdict(record)
        task["strategy_task_generations"] = {sid: saved["generation"]}
        task["strategy_task_results"] = current["strategy_task_results"]
        task["strategy_task_receipts"][sid] = task_receipt(owned.documents, sid, NOW.date())
        task["strategy_task_windows"][sid]["buy"][0]["observe_breadth"] = True
        task["strategy_task_windows"][sid]["buy"][0]["signal"]["params"]["pool_up_min_pct"] = None
        with owned.documents.transaction(write=False) as unit:
            task["_configuration_fingerprint"] = fingerprint(unit.connection, include_state=False)
        return account, context, task, observed

    monkeypatch.setattr(owned.worker, "prepare", prepare)
    return sid, record


def test_worker_commits_stock_archive_and_event_while_breadth_is_blocked(owned, monkeypatch):
    sid, record = configure(owned, monkeypatch)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def execute(task, **kwargs):
        phase = "breadth" if task["kind"] == "monitor_breadth" else "stocks"
        if phase == "breadth":
            entered.set()
            assert release.wait(3)
        result = reply(task, phase)
        result["results"][sid].update(
            monitor_contract="strategy_monitor_v2", runtime_identity=record.runtime_identity)
        if phase == "stocks":
            result["events"] = [{"type": "buy_signal", "strategy_id": sid, "source": "strategy",
                                 "symbol": "600001.SH", "name": "one", "signals": ["csgi_" + SIGNAL["id"]],
                                 "ts": int(NOW.timestamp() * 1000), "severity": "info", "message": "test signal"}]
        return result

    owned.worker.executor = SimpleNamespace(execute=execute, isolated=True)

    def evaluate():
        try:
            owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
            returned.set()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=evaluate)
    thread.start()
    try:
        assert entered.wait(1)
        assert returned.wait(.5), "Worker retained the tenant cycle while breadth waited"
        thread.join(1)
        for _ in range(10):
            owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
            saved = task_receipt(owned.documents, sid, NOW.date())
            if saved.get("tasks", {}).get("buy", {}).get("terminal"):
                break
            threading.Event().wait(.02)
        assert not release.is_set() and not errors
        assert saved["tasks"]["buy"]["terminal"]
        assert StrategyResults(owned.documents).read(record, str(NOW.date()))
        assert any(e["type"] == "buy_signal" for e in Alerts(owned.documents).list_recent())
    finally:
        release.set()
        thread.join(4)
        owned.worker.close()


def test_worker_does_not_ack_phase_before_transaction_commit(owned, monkeypatch):
    sid, _ = configure(owned, monkeypatch)
    assert hasattr(owned.worker, "phase_inbox"), "Worker has no transactional phase acknowledgement"
    calls = []

    def execute(task, **kwargs):
        phase = "breadth" if task["kind"] == "monitor_breadth" else "stocks"
        calls.append(phase)
        result = reply(task, phase)
        result["results"][sid]["monitor_contract"] = "strategy_monitor_v2"
        return result

    owned.worker.executor = SimpleNamespace(execute=execute, isolated=True)
    original = StrategyResults.save
    failures = []

    def reject_first(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise RuntimeError("injected archive rollback")
        return original(*args, **kwargs)

    monkeypatch.setattr(StrategyResults, "save", reject_first)
    tenant = owned.documents.context.tenant_id
    rolled_back = False
    try:
        for _ in range(10):
            try:
                owned.worker.evaluate(tenant, owned.snapshot())
            except RuntimeError as exc:
                assert "archive rollback" in str(exc)
                rolled_back = True
                assert not task_receipt(owned.documents, sid, NOW.date())
                break
            threading.Event().wait(.02)
        assert rolled_back
        assert owned.worker.phase_inbox.pending(tenant)
        for _ in range(10):
            owned.worker.evaluate(tenant, owned.snapshot())
            if task_receipt(owned.documents, sid, NOW.date()).get("tasks", {}).get("buy", {}).get("terminal"):
                break
            threading.Event().wait(.02)
        assert task_receipt(owned.documents, sid, NOW.date())["tasks"]["buy"]["terminal"]
        assert calls.count("stocks") == 1, "Rollback lost the phase receipt and reran computation"
    finally:
        owned.worker.close()


def test_same_s02_signal_has_one_alert_and_outbox_across_changed_attempts(owned, monkeypatch):
    from app.multitenancy import runtime
    from app.multitenancy.alerts import append_in_transaction
    from app.multitenancy.notifications import Notifications
    from app.services.strategy_input_manifest import digest
    sid = "custom_mvxxcp88"
    channel = Notifications(owned.env["db"]).save(owned.documents, owned.account, {
        "name": "dedup-test", "kind": "email", "destination": owned.account["email"], "enabled": True,
    })[0]
    owned.documents.put("notification_preferences", "default", {"default_channels": [channel["id"]]})
    task = {"tenant_id": owned.documents.context.tenant_id, "daily_identities": {sid: {
        "strategy_id": sid, "strategy_version": "same-source", "runtime_identity": "same-definition"}},
        "strategy_task_generations": {sid: 1}, "strategy_input_manifests": {sid: {
            "stocks": {"input_sha256": digest("same-input")}}}}
    events = []
    for attempt in (1, 2):
        event = {"type": "buy_signal", "strategy_id": sid, "source": "strategy",
                 "symbol": "600001.SH", "signals": ["csgi_late_session_strength"],
                 "ts": int(NOW.timestamp() * 1000) + attempt, "message": f"attempt {attempt}"}
        event["id"] = runtime.signal_event_identity(task, event, as_of=str(NOW.date()))
        events.append(event)
        with owned.documents.transaction() as unit:
            append_in_transaction(unit.connection, task["tenant_id"], [event])
    assert events[0]["id"] == events[1]["id"]
    with owned.documents.transaction(write=False) as unit:
        assert unit.connection.execute("SELECT count(*) AS n FROM business.alerts").fetchone()["n"] == 1
        assert unit.connection.execute("SELECT count(*) AS n FROM business.notification_outbox").fetchone()["n"] == 1
    task["strategy_task_generations"][sid] = 2
    assert runtime.signal_event_identity(task, events[0], as_of=str(NOW.date())) != events[0]["id"]


def test_s02_operational_records_roll_back_deduplicate_and_never_enqueue_notifications(owned):
    from test_s02_run_evidence import NOW as EVIDENCE_NOW
    from test_s02_run_evidence import payload as evidence_payload

    from app.services.s02_run_evidence import save_operational
    task = evidence_payload()
    with pytest.raises(RuntimeError, match="rollback"), owned.documents.transaction() as unit:
        save_operational(unit, task, EVIDENCE_NOW, {})
        raise RuntimeError("operational rollback")
    with owned.documents.transaction(write=False) as unit:
        assert unit.connection.execute(
            "SELECT count(*) AS n FROM business.documents WHERE namespace=%s",
            ("strategy_operational_records",)).fetchone()["n"] == 0
    for _ in range(2):
        with owned.documents.transaction() as unit:
            save_operational(unit, task, EVIDENCE_NOW, {})
    with owned.documents.transaction(write=False) as unit:
        assert unit.connection.execute(
            "SELECT count(*) AS n FROM business.documents WHERE namespace=%s",
            ("strategy_operational_records",)).fetchone()["n"] == 2
        assert unit.connection.execute("SELECT count(*) AS n FROM business.notification_outbox").fetchone()["n"] == 0


def test_s02_phase_and_commit_provenance_survive_cold_day_view(owned):
    import gzip
    import json

    from test_strategy_input_manifest import AT, build, minutes

    from app.services.s02_run_evidence import record_phase
    from app.services.strategy_day_views import clear_memory, receipt_updated
    sid = "custom_mvxxcp88"
    value = reply(payload(), "stocks")["results"][SID]
    value.update(as_of=str(AT.date()), strategy_id=sid, monitor_contract="strategy_monitor_v2",
                 section_mode="signals", sections={"buy": value.get("rows", []), "select": [], "sell": []})
    task = {"strategy_input_manifests": {sid: {"stocks": build(minutes())}}}
    record_phase(value, task, "stocks", {"queue_ms": 1},
                 dispatched_at=AT.isoformat(), received_at=AT.isoformat())
    record = ResultIdentity(sid, "s02", "v1", "{}", "{}", "null", "stock", "1m")
    with owned.documents.transaction() as unit:
        saved = StrategyResults(unit).save(record, value, started_at=1)
        unit.put("strategy_run_commits", f"{sid}:{AT.date()}", {"phases": {"stocks": {
            "input_sha256": task["strategy_input_manifests"][sid]["stocks"]["input_sha256"],
            "commit_recorded_at": AT.isoformat()}}})
        receipt_updated(unit, sid, str(AT.date()))
    clear_memory()
    view = StrategyResults(owned.documents).day_view(sid, str(AT.date()))
    restored = json.loads(gzip.decompress(view[1]))["result"]
    assert restored["record_id"] == saved["record_id"]
    assert restored["run_evidence"]["phases"]["stocks"]["result_sha256"]
    assert restored["run_evidence"]["phases"]["stocks"]["input_sha256"] == (
        restored["run_commit"]["phases"]["stocks"]["input_sha256"])


def test_manual_s02_review_is_separate_and_never_changes_the_sell_roster_in_postgres(owned, monkeypatch):
    from test_s02_review_provenance import (
        test_manual_history_keeps_original_archive_and_next_day_sell_roster,
    )
    test_manual_history_keeps_original_archive_and_next_day_sell_roster(owned.documents, monkeypatch)
    with owned.documents.transaction(write=False) as unit:
        assert unit.connection.execute("SELECT count(*) AS n FROM business.alerts").fetchone()["n"] == 0
        assert unit.connection.execute("SELECT count(*) AS n FROM business.notification_outbox").fetchone()["n"] == 0
