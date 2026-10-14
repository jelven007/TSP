"""Persistent scheduling, authorization changes, prerequisites and tenant events."""
# ruff: noqa: F401, F811 -- isolated PostgreSQL fixture reuse.
import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from test_full_identity import env, full_cluster, register

from app.cell.context import AccessError
from app.cell.scheduler import Scheduler
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.market import encode_frame
from app.multitenancy.notifications import router as notifications
from app.multitenancy.scheduled_worker import TenantSchedules
from app.services import market_recap_reports

NOW = datetime(2026, 10, 14, 16, tzinfo=ZoneInfo("Asia/Shanghai"))


def own(env):
    token, user = register(env)
    env["client"].cookies.set("tf_session", token)
    context = env["identity"].context(user, None, "schedules")
    return user, TenantDocuments(env["db"], context)


def claim(env, documents):
    env["data"].execute("UPDATE scheduler.work SET due_at=NULL WHERE kind='scheduled'")
    env["data"].execute(
        "UPDATE scheduler.work SET due_at=now() WHERE kind='scheduled' AND tenant_id=%s",
        (documents.context.tenant_id,),
    )
    return Scheduler(env["db"]).claim("scheduled", 1)[0]


def configure(env):
    env["client"].app.include_router(notifications)
    user, documents = own(env)
    response = env["client"].post("/api/monitor-rules/notifications/review-schedule",
                                 json={"enabled": True, "hour": 15, "minute": 40})
    assert response.status_code == 200, response.text
    return user, documents


def snapshot():
    frame = encode_frame(pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 10, 14)]}))
    return {"frames": {asset: frame for asset in ("stock", "etf", "index")},
            "indices": encode_frame(pl.DataFrame()), "settlement_days": ["2026-10-14"]}


def test_review_restarts_do_not_duplicate_report_or_notification_and_events_are_private(env, tmp_path, monkeypatch):
    _, documents = configure(env)
    _, other = own(env)
    monkeypatch.setattr(market_recap_reports._store, "_path", lambda: pytest.fail("legacy report write"))
    calls = []
    async def stream(*args):
        calls.append(True)
        for event in ({"type": "meta", "as_of": "2026-10-14"}, {"type": "delta", "content": "租户复盘"},
                      {"type": "done"}):
            yield json.dumps(event)
    documents.put("notification_preferences", "default", {"review_push_mode": "auto"})
    service = TenantSchedules(env["db"], None, review_stream=stream)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), install_public_snapshot=lambda *a: None)
    try:
        asyncio.run(service.process(claim(env, documents), snapshot(), repo, now=NOW))
        asyncio.run(service.process(claim(env, documents), snapshot(), repo, now=NOW))
        assert len(calls) == 1
        assert len(market_recap_reports.list_reports(documents=documents)) == 1
        assert len(documents.records("notification_requests")) == 1
        assert other.records("notification_requests") == [] and market_recap_reports.list_reports(documents=other) == []
        with documents._transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM business.full_events").fetchone()["n"] >= 3
        with other._transaction() as conn:
            assert conn.execute("SELECT count(*) AS n FROM business.full_events").fetchone()["n"] == 0
    finally:
        service.runtime.close()


@pytest.mark.parametrize("change", ["password", "configuration", "lease"])
def test_review_discards_output_after_revoke_config_edit_or_expired_lease(env, tmp_path, change):
    user, documents = configure(env)
    async def stream(*args):
        yield '{"type":"meta","as_of":"2026-10-14"}'
        if change == "password":
            env["control"].execute("UPDATE control.users SET auth_version=auth_version+1 WHERE id=%s", (user["id"],))
        elif change == "configuration":
            row = documents.get("review_schedule", "default")
            documents.put("review_schedule", "default", {**row, "enabled": False})
        else:
            env["data"].execute("UPDATE scheduler.work SET lease_until=now()-interval '1 second' WHERE tenant_id=%s",
                                (documents.context.tenant_id,))
        yield '{"type":"delta","content":"必须丢弃"}'
        yield '{"type":"done"}'
    service = TenantSchedules(env["db"], None, review_stream=stream)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), install_public_snapshot=lambda *a: None)
    try:
        with pytest.raises(AccessError):
            asyncio.run(service.process(claim(env, documents), snapshot(), repo, now=NOW))
        assert market_recap_reports.list_reports(documents=documents) == []
        assert documents.records("notification_requests") == []
    finally:
        service.runtime.close()


def test_today_requires_completed_pipeline_before_scheduled_review(env, tmp_path):
    _, documents = configure(env)
    async def stream(*args):
        pytest.fail("review ran before public settlement publication")
        yield ""
    service = TenantSchedules(env["db"], None, review_stream=stream)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    try:
        asyncio.run(service.process(claim(env, documents), {**snapshot(), "settlement_days": []}, repo, now=NOW))
        assert documents.records("scheduled_jobs") == []
    finally:
        service.runtime.close()


def test_weekly_mining_has_one_durable_claim_and_skips_missing_prerequisites(env, tmp_path, monkeypatch):
    from app.api import mining
    from app.multitenancy import scheduled_worker

    env["client"].app.include_router(mining.router)
    _, documents = own(env)
    response = env["client"].patch("/api/backtest/mining/config", json={
        "mining_schedule_enabled": True, "mining_schedule_weekday": 2, "mining_budget_profile": "balanced",
    })
    assert response.status_code == 200, response.text
    monkeypatch.setattr(scheduled_worker, "build_data_fingerprint", lambda *a: {"digest": "same-data"})
    monkeypatch.setattr(scheduled_worker, "_prerequisite_error", lambda *a: "insufficient trading dates")
    service = TenantSchedules(env["db"], None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), latest_enriched_date=lambda *a: date(2026, 10, 14))
    try:
        asyncio.run(service.process(claim(env, documents), snapshot(), repo, now=NOW))
        asyncio.run(service.process(claim(env, documents), snapshot(), repo, now=NOW))
        with documents._transaction() as conn:
            rows = conn.execute("SELECT * FROM business.research_jobs").fetchall()
        assert len(rows) == 1 and rows[0]["state"] == "skipped_prerequisite"
        assert rows[0]["input"]["source_claim"] == "weekly-2026-W42"
        assert rows[0]["error"] == "insufficient trading dates"
    finally:
        service.runtime.close()
