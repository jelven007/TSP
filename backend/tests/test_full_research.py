"""Real PostgreSQL regression for durable full-product research and candidates."""
from __future__ import annotations

import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("psycopg")
from psycopg.errors import InsufficientPrivilege
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.multitenancy import access
from app.multitenancy.candidates import SQLCandidateStore, SQLMiningCandidates
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.research import ResearchJobs, ResearchWorker, SQLMiningManager, read_frame
from app.multitenancy.strategies import TenantStrategyEngine, definition_payload
from app.strategy.engine import StrategyEngine


def tenant(env):
    token, user = register(env)
    context = env["identity"].context(user, None, "research-test")
    docs = TenantDocuments(env["db"], context)
    return token, user, ResearchJobs(docs)


def payload(jobs, kind="backtest", **values):
    return {"kind": kind, "tenant_id": jobs.context.tenant_id, "config": {"period": "2026-10-14"},
            "sources": [], "overrides": {}, "factors": [], "signals": [], **values}


class Compute:
    def __init__(self, result=None):
        self.result = result or {"stats": {"total_return": .25}, "trades": []}
        self.calls = []

    def execute(self, task, progress, cancel):
        assert not cancel.is_set()
        self.calls.append(task)
        progress({"day": 1, "total": 2, "equity": 100})
        return self.result


def test_history_restores_original_stream_and_cannot_read_or_cancel_another_tenant(env):
    from app.api.backtest import router

    env["client"].app.include_router(router)
    token, _, jobs = tenant(env)
    other_token, _, _ = tenant(env)
    row = jobs.submit(payload(jobs), key="history-stream")
    worker = ResearchWorker(env["db"], Compute())
    try:
        worker.run(jobs.context, worker.claim(jobs.context))
    finally:
        worker.close()
    client = env["client"]
    client.cookies.set("tf_session", token)
    assert client.get("/api/backtest/jobs").json()["items"][0]["id"] == row["id"]
    details = client.get("/api/backtest/jobs/" + row["id"]).json()
    assert details["state"] == "succeeded" and details["result"]["stats"]["total_return"] == .25
    response = client.get("/api/backtest/jobs/" + row["id"] + "/stream")
    assert response.status_code == 200 and "event: done" in response.text and '"total_return": 0.25' in response.text
    client.cookies.set("tf_session", other_token)
    assert client.get("/api/backtest/jobs").json() == {"items": []}
    for suffix in ("", "/stream"):
        assert client.get("/api/backtest/jobs/" + row["id"] + suffix).status_code == 404
    assert client.post("/api/backtest/jobs/" + row["id"] + "/cancel").status_code == 404
    assert client.delete("/api/backtest/jobs/" + row["id"]).status_code == 404
    client.cookies.set("tf_session", token)
    assert client.delete("/api/backtest/jobs/" + row["id"]).status_code == 200


def test_same_parameters_are_owned_by_tenant_and_duplicate_submits_are_atomic(env):
    _, _, a = tenant(env)
    _, _, b = tenant(env)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: a.submit(payload(a), key="same-ui-key"), range(4)))
    assert len({row["id"] for row in rows}) == 1
    other = b.submit(payload(b), key="same-ui-key")
    assert other["id"] != rows[0]["id"]
    assert b.get(rows[0]["id"]) is None
    assert b.cancel(rows[0]["id"]) is False
    assert b.cancel_key("same-ui-key", "backtest") is True
    assert a.get(rows[0]["id"])["state"] == "queued"
    assert a.cancel_key("same-ui-key", "backtest") is True


def test_triggered_scheduler_skips_empty_tenants_and_advances_every_queued_tenant(env):
    empty = [tenant(env)[2] for _ in range(4)]
    queued = [tenant(env)[2] for _ in range(8)]
    rows = [jobs.submit(payload(jobs), force=True) for jobs in queued]
    empty_ids = [jobs.context.tenant_id for jobs in empty]
    queued_ids = [jobs.context.tenant_id for jobs in queued]
    work = env["data"].execute(
        """SELECT tenant_id FROM scheduler.work
           WHERE kind='research' AND tenant_id=ANY(%s) AND due_at IS NOT NULL""",
        (empty_ids + queued_ids,),
    ).fetchall()
    assert {row["tenant_id"] for row in work} == set(queued_ids)

    compute = Compute()
    worker = ResearchWorker(env["db"], compute, concurrency=2)
    worker.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if all(jobs.get(row["id"])["state"] == "succeeded"
                   for jobs, row in zip(queued, rows, strict=True)):
                break
            time.sleep(.05)
        assert all(jobs.get(row["id"])["state"] == "succeeded"
                   for jobs, row in zip(queued, rows, strict=True))
        assert {task["tenant_id"] for task in compute.calls} == set(queued_ids)
    finally:
        worker.close()


def test_private_source_or_date_change_never_reuses_completed_result(env):
    _, _, jobs = tenant(env)
    row = jobs.submit(payload(jobs), key="stable-key")
    compute = Compute()
    worker = ResearchWorker(env["db"], compute)
    try:
        claimed = worker.claim(jobs.context)
        worker.run(jobs.context, claimed)
        assert jobs.get(row["id"])["state"] == "succeeded"
        assert jobs.submit(payload(jobs), key="stable-key")["id"] == row["id"]
        changed = jobs.submit(payload(jobs, factors=[{"id": "uf_private", "formula": "close * 2"}]),
                              key="stable-key")
        assert changed["id"] != row["id"]
        jobs.cancel(changed["id"])
        changed_date = jobs.submit(payload(jobs, config={"period": "2026-10-15"}), key="stable-key")
        assert changed_date["id"] != row["id"]
        jobs.cancel(changed_date["id"])
    finally:
        worker.close()


def test_research_config_and_trial_family_survive_restart_without_cross_tenant_reuse(env):
    from copy import deepcopy

    _, _, jobs = tenant(env)
    _, _, other = tenant(env)
    config = {"strategy_id": "s", "param_grid": {"p": [1, 2]},
              "overrides": {"overlay_filter": [{"left": "close", "op": ">", "right": "10"}]}}
    task = payload(jobs, kind="optimize", config=config, public_release_id="frozen-market-v1")
    original = deepcopy(task)
    row = jobs.submit(task, key="frozen-family")
    config["overrides"]["overlay_filter"][0]["right"] = "99"
    assert jobs.get(row["id"])["input"] == original
    assert row["metadata"]["research"]["input_snapshot"] == row["metadata"]["snapshot"]
    assert row["metadata"]["research"]["public_release_id"] == "frozen-market-v1"
    assert jobs.submit(original, key="frozen-family")["id"] == row["id"]
    changed = jobs.submit(task, key="frozen-family")
    assert changed["id"] != row["id"]
    jobs.cancel(changed["id"])
    compute = Compute({"research": {"family_id": "actual-grid"},
                       "results": [{"trial_id": "actual-grid:1", "status": "succeeded",
                                    "full_params": {"p": 1}, "objective_raw": .5}]})
    worker = ResearchWorker(env["db"], compute)
    try:
        worker.run(jobs.context, worker.claim(jobs.context))
    finally:
        worker.close()
    restored = ResearchJobs(TenantDocuments(env["db"], jobs.context)).get(row["id"])
    assert compute.calls[0]["config"] == original["config"]
    assert restored["result"]["research"]["provenance"] == row["metadata"]["research"]
    assert restored["result"]["results"][0]["full_params"] == {"p": 1}
    assert other.get(row["id"]) is None
    assert other.submit({**original, "tenant_id": other.context.tenant_id}, key="frozen-family")["id"] != row["id"]


def test_result_and_progress_recover_in_new_store_and_expired_attempt_cannot_commit(env):
    _, _, jobs = tenant(env)
    first = jobs.submit(payload(jobs))
    worker = ResearchWorker(env["db"], Compute())
    try:
        claimed = worker.claim(jobs.context)
        assert worker.claim(jobs.context) is None
        worker.run(jobs.context, claimed)
        recovered = ResearchJobs(TenantDocuments(env["db"], jobs.context)).get(first["id"])
        assert recovered["result"]["stats"]["total_return"] == .25
        assert [event["type"] for event in recovered["events"]] == ["queued", "running", "progress", "succeeded"]
        row = jobs.submit(payload(jobs), force=True)
        expired = worker.claim(jobs.context)
        with env["db"].business_transaction(jobs.context, write=True) as conn:
            conn.execute("UPDATE business.research_jobs SET lease_until=now()-interval '1 second' WHERE id=%s",
                         (row["id"],))
        expiration = jobs.get(row["id"])["lease_until"]
        worker.run(jobs.context, expired)
        assert jobs.get(row["id"])["lease_until"] == expiration
        assert worker.claim(jobs.context) is None
        worker.finish(jobs, jobs.context, expired, {"stats": {"total_return": 999}}, None)
        assert jobs.get(row["id"])["state"] == "interrupted"
        assert jobs.get(row["id"])["result"] is None
    finally:
        worker.close()


def test_cancel_and_role_revocation_discard_late_results(env):
    _, user, jobs = tenant(env)
    for revoke in (False, True):
        row = jobs.submit(payload(jobs), force=True)

        class Revoked(Compute):
            def execute(self, task, progress, cancel, *, revoke=revoke, job_id=row["id"]):
                if revoke:
                    env["control"].execute("UPDATE control.users SET auth_version=auth_version+1 WHERE id=%s",
                                           (user["id"],))
                else:
                    jobs.cancel(job_id)
                return {"stats": {"total_return": 999}, "artifact_data": {"candidates": "untrusted"}}

        worker = ResearchWorker(env["db"], Revoked())
        try:
            claimed = worker.claim(jobs.context)
            worker.run(jobs.context, claimed)
            result = jobs.get(row["id"])
            assert result["state"] == ("failed" if revoke else "cancelled")
            assert result["result"] is None
            with pytest.raises(AccessError):
                jobs.artifact(row["id"], "candidates")
        finally:
            worker.close()


def test_busy_broker_requeues_and_fences_previous_owner(env):
    _, _, jobs = tenant(env)
    row = jobs.submit(payload(jobs))

    class Busy(Compute):
        def execute(self, task, progress, cancel):
            raise AccessError(429, "busy")

    worker = ResearchWorker(env["db"], Busy())
    try:
        old = worker.claim(jobs.context)
        worker.run(jobs.context, old)
        assert jobs.get(row["id"])["state"] == "queued"
        new = worker.claim(jobs.context)
        assert old["lease_owner"] != new["lease_owner"]
        worker.finish(jobs, jobs.context, old, {"stats": {"total_return": 999}}, None)
        assert jobs.get(row["id"])["state"] == "running"
        jobs.cancel(row["id"])
        worker.run(jobs.context, new)
    finally:
        worker.close()


def test_artifacts_are_sql_owned_and_candidates_preserve_validation_and_concurrent_writes(env):
    _, _, a = tenant(env)
    _, _, b = tenant(env)
    output = io.BytesIO()
    pl.DataFrame({"private_result": ["tenant-a"]}).write_parquet(output)
    result = {"status": "succeeded", "artifacts": ["factors"],
              "artifact_data": {"factors": base64.b64encode(output.getvalue()).decode()}}
    row = a.submit(payload(a, kind="mining"))
    worker = ResearchWorker(env["db"], Compute(result))
    try:
        worker.run(a.context, worker.claim(a.context))
        assert a.frame(row["id"], "factors").item() == "tenant-a"
        with pytest.raises(AccessError):
            b.artifact(row["id"], "factors")
        candidates = SQLCandidateStore(a.documents)
        with ThreadPoolExecutor(max_workers=4) as pool:
            saved = list(pool.map(lambda i: candidates.create(
                kind="factor", name=f"candidate-{i}", source_id="rsi_14",
                config={"factor_name": "rsi_14"}, metrics={"ic_mean": .03}, data_as_of="2026-10-14",
            ), range(8)))
        assert len(candidates.list()) == 8
        assert SQLCandidateStore(b.documents).list() == []
        with pytest.raises(KeyError):
            SQLCandidateStore(b.documents).update(saved[0]["id"], name="other-tenant")
        assert candidates.update(saved[0]["id"], status="validated")["status"] == "validated"
    finally:
        worker.close()


def test_mining_reuse_includes_private_snapshot_and_retained_progress(env):
    _, _, jobs = tenant(env)

    class Engine:
        def task(self, kind, **values):
            return payload(jobs, kind, **values)

    manager = SQLMiningManager(jobs, Engine())
    request = {"factor_names": ["rsi_14"], "asset_type": "stock"}
    fingerprint = {"generation": "published-generation"}
    first = manager.start(request, fingerprint)
    second = manager.start(request, fingerprint)
    assert first["run_id"] == second["run_id"]
    assert second["reused"] is True
    assert manager.store.list_runs()[0]["run_id"] == first["run_id"]
    assert manager.store.read_events(first["run_id"])[0]["type"] == "queued"
    assert manager.cancel(first["run_id"])["status"] == "cancelled"


def test_real_rls_hides_jobs_artifacts_without_context(env):
    _, _, jobs = tenant(env)
    jobs.submit(payload(jobs))
    with env["db"].data.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM business.research_jobs").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM business.research_artifacts").fetchone()["n"] == 0
        with pytest.raises(InsufficientPrivilege):
            conn.execute(
                """INSERT INTO business.research_artifacts(tenant_id,job_id,name,content)
                   VALUES(%s,'foreign','factors',%s)""", (jobs.context.tenant_id, b"private"),
            )


def test_artifact_limits_are_checked_before_decompression(monkeypatch):
    output = io.BytesIO()
    pl.DataFrame({"result": ["large repeated value" * 1000] * 2}).write_parquet(output)
    data = output.getvalue()
    monkeypatch.setattr(pl, "read_parquet", lambda *_: pytest.fail("artifact was decompressed before validation"))
    with pytest.raises(ValueError, match="expanded size limit"):
        read_frame(data, max_rows=1)
    with pytest.raises(ValueError, match="expanded size limit"):
        read_frame(data, max_uncompressed=1)


def test_original_streams_and_http_history_do_not_use_legacy_worker_or_job_table(env, monkeypatch):
    from app.api import backtest, mining
    from app.backtest import worker as legacy
    from app.config import settings

    builtins = StrategyEngine([Path(backtest.__file__).resolve().parents[1] / "strategy" / "builtin"])
    sid = builtins.strategy_definitions()[0].meta["id"]
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.state.strategy_engine = builtins
    app.state.compute_executor = Compute()
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=Path("/unused-public")),
        earliest_daily_date=lambda: date(2026, 1, 1),
    )
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    app.include_router(backtest.router)
    app.include_router(mining.router)
    monkeypatch.setattr(settings, "backtest_range_guard", False)
    monkeypatch.setattr(legacy, "run_worker_task", lambda *_: pytest.fail("legacy compute was used"))
    initial = set(backtest._running_jobs)
    token, _, _ = tenant(env)
    other_token, _, _ = tenant(env)
    worker = ResearchWorker(env["db"], app.state.compute_executor)
    worker.start()
    try:
        with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
            # Use the actual cookie name; no mock identity is installed.
            from app.api.auth import COOKIE_NAME

            client.cookies.set(COOKIE_NAME, token)
            for kind in ("strategy", "optimize", "walkforward"):
                response = client.get(f"/api/backtest/{kind}/stream", params={
                    "strategy_id": sid, "start": "2026-01-01", "end": "2026-03-01",
                    **({"param_grid": '{"test":[1]}'} if kind != "strategy" else {}),
                })
                assert response.status_code == 200, response.text
                assert "event: job" in response.text and "event: done" in response.text, response.text
            items = client.get("/api/backtest/jobs").json()["items"]
            assert len(items) == 3
            mine = items[0]["id"]
            client.cookies.set(COOKIE_NAME, other_token)
            assert client.get("/api/backtest/jobs").json()["items"] == []
            assert client.delete(f"/api/backtest/jobs/{mine}").status_code == 404
            client.cookies.set(COOKIE_NAME, token)
            assert client.delete(f"/api/backtest/jobs/{mine}").json() == {"ok": True}
            response = client.get("/api/backtest/optimize/stream", params={
                "strategy_id": sid, "param_grid": "not-json", "start": "2026-01-01", "end": "2026-03-01",
            })
            assert response.status_code == 400
            assert client.patch("/api/backtest/mining/config", json={
                "mining_schedule_enabled": True,
            }).status_code == 200
            client.cookies.set(COOKIE_NAME, other_token)
            assert client.get("/api/backtest/mining/config").json()["mining_schedule_enabled"] is False
        assert set(backtest._running_jobs) == initial
    finally:
        worker.close()


def test_mining_promote_publish_and_recovery_use_original_rules_with_sql_sources(env, tmp_path):
    from test_mining_candidates import _create_run

    from app.strategy.ai_generator import AIStrategyGenerator

    legacy, old_id, sig = _create_run(tmp_path)
    _, _, a = tenant(env)
    _, _, b = tenant(env)
    builtins = StrategyEngine([Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"])
    template = builtins.get("factor_rank_research")

    class Metadata:
        def execute(self, task):
            code = next(row["code"] for row in task["sources"] if row["id"] == task["strategy_id"])
            meta = AIStrategyGenerator._extract_meta(code)
            return {"definition": definition_payload(replace(template, meta=meta, source="custom"))}

    engine = TenantStrategyEngine(a.documents, builtins, Metadata())
    manager = SQLMiningManager(a, engine)
    submitted = manager.start(legacy.get(old_id)["request"], {"generation": "fixture"})
    output = {**legacy.read_summary(old_id), "status": "succeeded", "artifacts": ["candidates"],
              "artifact_data": {"candidates": base64.b64encode(
                  legacy.artifact_path(old_id, "candidates").read_bytes()).decode()}}
    worker = ResearchWorker(env["db"], Compute(output))
    try:
        worker.run(a.context, worker.claim(a.context))
        service = SQLMiningCandidates(a.documents, manager.store, engine)
        promoted = service.promote(submitted["run_id"], sig)
        assert service.promote(submitted["run_id"], sig)["id"] == promoted["id"]
        with pytest.raises(KeyError):
            SQLMiningCandidates(b.documents, SQLMiningManager(b, engine).store, engine).promote(
                submitted["run_id"], sig,
            )
        published = service.publish(submitted["run_id"], sig)
        assert service.publish(submitted["run_id"], sig) == published
        assert a.documents.get("strategy_sources", published["strategy_id"])["source"] == "custom"
        assert b.documents.values("strategy_sources") == []
        assert manager.store.read_artifact(submitted["run_id"], "candidates")["promoted_candidate_id"][0] == promoted["id"]
        assert len(SQLCandidateStore(a.documents).list()) == 1
    finally:
        worker.close()
