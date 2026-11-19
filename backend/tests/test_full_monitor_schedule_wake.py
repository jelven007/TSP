"""Real SQL wake-up without tenant scans or bypassing active leases."""
# ruff: noqa: F401,F811 -- isolated PostgreSQL fixture reuse.
from datetime import UTC, datetime, timedelta

import pytest
from test_full_identity import env, full_cluster
from test_full_schedules import own

from app.cell.admin import migrate
from app.cell.scheduler import Scheduler
from app.services.strategy_monitor_tasks import set_monitoring


@pytest.fixture(scope="module", autouse=True)
def upgrade(full_cluster):
    for kind, role in zip(("control", "data"), full_cluster["roles"], strict=True):
        migrate(full_cluster[kind], kind, role, "full", target_version=9)
    full_cluster["db"].settings = full_cluster["db"].settings.model_copy(update={"schema_version": 9})
    full_cluster["db"].check()


def defer(env, documents):
    env["data"].execute(
        "UPDATE scheduler.work SET due_at=now()+interval '1 minute' "
        "WHERE kind='scheduled' AND tenant_id=%s", (documents.context.tenant_id,))


def state(env, documents):
    return env["data"].execute(
        "SELECT * FROM scheduler.work WHERE kind='scheduled' AND tenant_id=%s",
        (documents.context.tenant_id,)).fetchone()


def test_enable_wakes_only_its_tenant_and_rollback_does_not_wake(env):
    _, documents = own(env)
    _, other = own(env)
    defer(env, documents)
    defer(env, other)
    before = state(env, documents)
    with pytest.raises(ValueError), documents.transaction() as unit:
        set_monitoring(unit, "custom_alpha", "Alpha", True)
        raise ValueError("rollback")
    assert state(env, documents) == before
    set_monitoring(documents, "custom_alpha", "Alpha", True)
    after = state(env, documents)
    assert after["due_at"] <= datetime.now(UTC)
    assert after["revision"] > before["revision"]
    assert state(env, other)["due_at"] > datetime.now(UTC)
    set_monitoring(documents, "custom_alpha", "Alpha", False)
    assert not documents.get("strategy_monitoring", "custom_alpha")["enabled"]


def test_startup_resumes_delayed_work_without_stealing_active_lease(env):
    _, documents = own(env)
    scheduler = Scheduler(env["db"])
    env["data"].execute("UPDATE scheduler.work SET due_at=NULL WHERE kind='scheduled'")
    env["data"].execute(
        "UPDATE scheduler.work SET due_at=now() WHERE kind='scheduled' AND tenant_id=%s",
        (documents.context.tenant_id,))
    lease = scheduler.claim("scheduled", 1)[0]
    try:
        before = state(env, documents)
        scheduler.resume_scheduled()
        after = state(env, documents)
        assert after["owner"] == before["owner"]
        assert after["lease_until"] == before["lease_until"]
        assert scheduler.claim("scheduled", 1) == []
        set_monitoring(documents, "custom_beta", "Beta", True)
        assert scheduler.finish(lease, datetime.now(UTC) + timedelta(minutes=1))
        assert state(env, documents)["due_at"] <= datetime.now(UTC)
        next_lease = scheduler.claim("scheduled", 1)[0]
        assert scheduler.finish(next_lease, None)
        defer(env, documents)
        scheduler.resume_scheduled()
        assert state(env, documents)["due_at"] <= datetime.now(UTC)
    finally:
        scheduler.finish(lease, None)
