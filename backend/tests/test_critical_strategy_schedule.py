"""Durable critical monitoring must survive ordinary rule cleanup."""
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
from app.services.strategy_resources import BudgetPermits


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


def test_deleted_critical_registration_is_restored_and_restart_is_idempotent(owned):
    from app.multitenancy.critical_schedule import reconcile

    sid = configure(owned)
    reconcile(owned.worker, owned.documents)
    rules = Rules(owned.documents)
    saved = rules.load_one("critical_test")
    assert saved["enabled"] and saved["strategy_id"] == sid
    assert saved["direction"] == "both" and saved["scope"] == "all"
    before = rules.load_all()
    reconcile(owned.worker, owned.documents)
    assert rules.load_all() == before
    rules.delete_one(saved["id"], managed=True)
    reconcile(owned.worker, owned.documents)
    restored = rules.load_one(saved["id"])
    assert {k: v for k, v in restored.items() if k != "created_at"} == {
        k: v for k, v in saved.items() if k != "created_at"
    }
    with owned.documents.transaction(write=False) as unit:
        actions = unit.connection.execute(
            "SELECT action FROM business.audit WHERE target_id=%s", (saved["id"],),
        ).fetchall()
    assert {a["action"] for a in actions} >= {"monitor.save", "monitor.delete"}


@pytest.mark.parametrize("operation", ["delete", "disable", "redirect"])
def test_ordinary_cleanup_cannot_remove_or_redirect_critical_task(owned, operation):
    rules = Rules(owned.documents)
    from app.strategy.monitor_rules import normalize

    rules.save_one(normalize({
        "id": "critical_test", "name": "S02", "type": "strategy", "scope": "all",
        "strategy_id": "custom_test", "_system_task": "late_session_critical_v1",
    }))
    saved = rules.load_one("critical_test")
    with pytest.raises(AccessError, match="系统关键任务"):
        if operation == "delete":
            rules.delete_one(saved["id"])
        else:
            changes = {"enabled": False} if operation == "disable" else {"strategy_id": "other"}
            rules.save_one({**saved, **changes})
    assert rules.load_one(saved["id"]) == saved


def test_ordinary_rules_remain_deletable(owned):
    rules = Rules(owned.documents)
    rules.save_one(price_rule())
    assert rules.delete_one("my_price")


@pytest.mark.parametrize("missing", ["source", "signal", "disabled_signal", "disabled_schedule"])
def test_critical_schedule_never_revives_unavailable_inputs(owned, missing):
    from app.multitenancy.critical_schedule import reconcile

    sid = configure(owned)
    if missing == "source":
        owned.documents.delete("strategy_sources", sid)
    elif missing == "signal":
        owned.documents.delete("custom_signals", "late_session_strength")
    elif missing == "disabled_signal":
        signal = owned.documents.get("custom_signals", "late_session_strength")
        owned.documents.put("custom_signals", signal["id"], {**signal, "enabled": False})
    else:
        schedule = owned.documents.get("critical_strategy_schedule", sid)
        owned.documents.put("critical_strategy_schedule", sid, {**schedule, "enabled": False})
    before = owned.documents.values("custom_signals")
    reconcile(owned.worker, owned.documents)
    assert Rules(owned.documents).load_all() == []
    assert owned.documents.values("custom_signals") == before


def test_expired_scheduler_claim_cannot_restore_a_task(owned):
    from app.multitenancy.critical_schedule import reconcile

    configure(owned)
    claim = {"tenant_id": owned.documents.context.tenant_id, "kind": "realtime",
             "owner": "expired", "revision": 1}
    with pytest.raises(AccessError, match="租约"):
        reconcile(owned.worker, owned.documents, claim)
    assert Rules(owned.documents).load_all() == []


@pytest.mark.parametrize("hour,minute,second,expected", [
    (14, 24, 59, 0), (14, 25, 0, 1), (14, 30, 30, 1),
    (14, 40, 0, 1), (14, 55, 0, 1), (14, 55, 1, 0),
])
def test_compute_reserves_critical_capacity_through_entry_deadline(
    tmp_path, monkeypatch, hour, minute, second, expected,
):
    monkeypatch.setattr(compute_broker, "cn_now", lambda: datetime(
        2026, 11, 11, hour, minute, second, tzinfo=CN_TZ))
    with TemporaryDirectory(dir="/tmp", prefix="tsp-c-") as short:
        broker = compute_broker.ComputeBroker(
            Path(short) / "b.sock", "image", tmp_path, 1,
            host_memory_mb=16384, host_cpus=8,
        )
        try:
            policy = broker._resource_policy()
            assert policy["effective_concurrency"] == 1
            assert policy["realtime_reserved_slots"] == expected
            if expected:
                permits = BudgetPermits(lambda: policy)
                assert not permits.acquire(blocking=False)
                assert permits.acquire(blocking=False, priority=True)
                permits.release()
        finally:
            broker.server_close()


def test_weekend_does_not_reserve_trade_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(compute_broker, "cn_now", lambda: datetime(
        2026, 11, 14, 14, 30, tzinfo=CN_TZ))
    with TemporaryDirectory(dir="/tmp", prefix="tsp-c-") as short:
        broker = compute_broker.ComputeBroker(
            Path(short) / "b.sock", "image", tmp_path, 1,
            host_memory_mb=16384, host_cpus=8,
        )
        try:
            assert broker._resource_policy()["realtime_reserved_slots"] == 0
        finally:
            broker.server_close()


def test_background_started_before_window_yields_and_monitor_gets_slot(tmp_path, monkeypatch):
    import socket
    import subprocess
    import sys
    import threading
    from types import SimpleNamespace

    from app.multitenancy.executor import ComputeExecutor

    clock = [datetime(2026, 11, 11, 14, 24, 59, tzinfo=CN_TZ)]
    monkeypatch.setattr(compute_broker, "cn_now", lambda: clock[0])
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    started = threading.Event()
    failures = []
    with TemporaryDirectory(dir="/tmp", prefix="tsp-c-") as short:
        path = Path(short) / "b.sock"
        with compute_broker.ComputeBroker(
            path, "image", tmp_path, 1, host_memory_mb=16384, host_cpus=8,
        ) as broker:
            def command(*_args):
                started.set()
                if broker.permits.background_active:
                    return [sys.executable, "-c", "import time; time.sleep(30)"]
                return [sys.executable, "-c", 'print(\'{"type":"result","payload":{"events":[]}}\')']

            monkeypatch.setattr(broker, "command", command)
            server = threading.Thread(target=broker.serve_forever, daemon=True)
            server.start()

            def background():
                try:
                    ComputeExecutor(str(path)).execute({"kind": "strategy", "tenant_id": "same"})
                except AccessError as exc:
                    failures.append(exc.status)

            task = threading.Thread(target=background)
            task.start()
            try:
                assert started.wait(3)
                clock[0] = datetime(2026, 11, 11, 14, 25, tzinfo=CN_TZ)
                task.join(4)
                assert not task.is_alive(), "background kept the critical slot"
                assert failures == [429]
                result = ComputeExecutor(str(path)).execute({"kind": "monitor", "tenant_id": "same"})
                assert result["events"] == []
                assert broker.permits.usage()["active"] == 0
                assert not broker.active_tenants
            finally:
                if task.is_alive():
                    broker.stopped.set()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                        connection.connect(str(path))
                    task.join(3)
                broker.shutdown()
                server.join(3)


def test_frozen_owner_cannot_restore_critical_schedule(owned):
    from app.multitenancy.critical_schedule import reconcile

    configure(owned)
    with owned.worker.db.control.connection() as conn:
        conn.execute("UPDATE control.users SET account_state='suspended' WHERE id=%s",
                     (owned.documents.context.user_id,))
    with pytest.raises(AccessError, match="账户不可用"):
        reconcile(owned.worker, owned.documents)
    assert Rules(owned.documents).load_all() == []


def test_historical_account_settlement_waits_until_critical_window_ends(monkeypatch):
    from types import SimpleNamespace

    from app.multitenancy import runtime

    calls = []
    session = SimpleNamespace(account_ids=lambda: calls.append("settle") or [])
    monkeypatch.setattr(runtime, "cn_now", lambda: datetime(2026, 11, 11, 14, 30, 30, tzinfo=CN_TZ))
    runtime.FullWorker.settle(session, {})
    assert calls == []
    monkeypatch.setattr(runtime, "cn_now", lambda: datetime(2026, 11, 11, 14, 55, 1, tzinfo=CN_TZ))
    runtime.FullWorker.settle(session, {})
    assert calls == ["settle"]


def test_native_api_cannot_delete_or_disable_system_task(app_env):
    from app.strategy.monitor_rules import normalize

    documents = app_env["own"]
    rule = normalize({"id": "critical_api", "name": "S02", "type": "strategy",
                      "strategy_id": "custom_test", "scope": "all",
                      "_system_task": "late_session_critical_v1"})
    Rules(documents).save_one(rule, system=True)
    client = app_env["client"]
    assert client.delete("/api/monitor-rules/critical_api").status_code == 409
    # The fixture has no strategy compute engine; redirecting to a valid price
    # shape must still hit the persisted system guard before any write.
    assert client.post("/api/monitor-rules", json={
        **rule, "type": "price", "scope": "symbols", "symbols": ["600519.SH"],
        "conditions": [{"field": "close", "op": ">", "value": 10}], "enabled": False,
    }).status_code == 409
    assert Rules(documents).load_one("critical_api")["enabled"]
