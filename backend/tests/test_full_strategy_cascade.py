"""Real SQL/RLS and lease-fencing acceptance for strategy cascades."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register
from test_strategy_delete import _strategy_code

from app.cell.context import AccessError
from app.multitenancy.candidates import SQLCandidateStore
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.research import ResearchJobs, ResearchWorker
from app.multitenancy.strategies import TenantStrategyEngine, definition_payload
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy.engine import StrategyEngine


def catalog(env, tmp_path):
    _, account = register(env)
    docs = TenantDocuments(env["db"], env["identity"].context(account, None, "cascade"))
    for sid in ("custom_target", "custom_other"):
        path = tmp_path / f"{sid}.py"
        code = _strategy_code(sid)
        path.write_text(code)
        definition = StrategyEngine._load_file(path)
        docs.put("strategy_sources", sid, {
            "source": "custom", "code": code, "definition": definition_payload(definition),
        })
    engine = TenantStrategyEngine(docs, StrategyEngine([]), SimpleNamespace())
    return docs, engine


def test_sql_deletion_cascades_history_jobs_artifacts_and_candidates(env, tmp_path):
    docs, engine = catalog(env, tmp_path)
    foreign, _ = catalog(env, tmp_path)
    key = ResultIdentity("custom_target", "策略", "v1", "{}", "{}", "null", "stock", "1d")
    store = StrategyResults(docs)
    for day in ("2026-10-20", "2026-10-21"):
        store.save(key, {"as_of": day, "rows": []}, started_at=1)
        store.save(replace(key, timeframe="1m"), {"as_of": day, "rows": []}, started_at=1)
    other = replace(key, strategy_id="custom_other")
    store.save(other, {"as_of": "2026-10-20", "rows": []}, started_at=1)
    StrategyResults(foreign).save(key, {"as_of": "2026-10-20", "rows": []}, started_at=1)
    jobs = ResearchJobs(docs)
    rows = [jobs.submit(engine.task(kind, config={"strategy_id": key.strategy_id}), force=True)
            for kind in ("backtest", "optimize", "walkforward")]
    unrelated = jobs.submit(engine.task("backtest", config={"strategy_id": other.strategy_id}), force=True)
    with docs._transaction(write=True) as conn:
        conn.execute("UPDATE business.research_jobs SET state='succeeded' WHERE id=%s", (rows[0]["id"],))
        conn.execute(
            "INSERT INTO business.research_artifacts(tenant_id,job_id,name,content) VALUES(%s,%s,%s,%s)",
            (docs.context.tenant_id, rows[0]["id"], "folds", b"test-artifact"),
        )
    candidate = SQLCandidateStore(docs).create(
        kind="strategy", name="目标候选", source_id="backtest",
        config={"strategy_id": key.strategy_id, "research_job_id": rows[0]["id"]},
        metrics={}, data_as_of=None,
    )
    retained = SQLCandidateStore(docs).create(
        kind="strategy", name="其他候选", source_id="backtest",
        config={"strategy_id": other.strategy_id}, metrics={}, data_as_of=None,
    )
    engine.delete_source(key.strategy_id)
    assert docs.get("strategy_sources", key.strategy_id) is None
    assert store.list_history(key.strategy_id)["records"] == []
    assert store.read(other, "2026-10-20") is not None
    assert StrategyResults(foreign).read(key, "2026-10-20") is not None
    assert all(jobs.get(row["id"]) is None for row in rows)
    assert jobs.get(unrelated["id"]) is not None
    assert candidate["id"] not in [item["id"] for item in SQLCandidateStore(docs).list()]
    assert SQLCandidateStore(docs).list() == [retained]
    with pytest.raises(AccessError) as error:
        jobs.artifact(rows[0]["id"], "folds")
    assert error.value.status == 404


def test_sql_deleted_running_job_cannot_commit_late_result(env, tmp_path):
    docs, engine = catalog(env, tmp_path)
    jobs = ResearchJobs(docs)
    row = jobs.submit(engine.task("backtest", config={"strategy_id": "custom_target"}))
    worker = ResearchWorker(env["db"], SimpleNamespace())
    try:
        claimed = worker.claim(docs.context)
        assert claimed["id"] == row["id"]
        engine.delete_source("custom_target")
        worker.finish(jobs, docs.context, claimed, {"stats": {}, "trades": []}, None)
        assert jobs.get(row["id"]) is None
    finally:
        worker.close()
    with pytest.raises(AccessError) as error:
        jobs.submit(claimed["input"], force=True)
    assert error.value.status == 409


def test_sql_cleanup_failure_rolls_back_source_history_and_generation(env, tmp_path, monkeypatch):
    docs, engine = catalog(env, tmp_path)
    key = ResultIdentity("custom_target", "策略", "v1", "{}", "{}", "null", "stock", "1d")
    store = StrategyResults(docs)
    store.save(key, {"as_of": "2026-10-20", "rows": [{"symbol": "A"}]}, started_at=1)
    original = ResearchJobs.delete_strategy

    def fail(self, *args):
        original(self, *args)
        raise RuntimeError("rollback")

    monkeypatch.setattr(ResearchJobs, "delete_strategy", fail)
    with pytest.raises(RuntimeError, match="rollback"):
        engine.delete_source(key.strategy_id)
    assert engine.has(key.strategy_id) and docs.get("strategy_sources", key.strategy_id) is not None
    assert store.read(key, "2026-10-20")["result"]["rows"] == [{"symbol": "A"}]
    store.save(key, {"as_of": "2026-10-21", "rows": []}, started_at=2)


def test_sql_history_generation_rollback_and_permission_isolation(env, tmp_path):
    docs, _ = catalog(env, tmp_path)
    key = ResultIdentity("custom_target", "策略", "v1", "{}", "{}", "null", "stock", "1d")
    store = StrategyResults(docs)
    store.save(key, {"as_of": "2026-10-20", "rows": []}, started_at=1)
    readonly = TenantDocuments(docs.db, replace(docs.context, permissions=frozenset({"business:read"})))
    with pytest.raises(AccessError), StrategyResults(readonly).deleting_strategy(key.strategy_id):
        pass
    with (pytest.raises(RuntimeError), docs.transaction() as session,
          StrategyResults(session).deleting_strategy(key.strategy_id)):
        assert StrategyResults(session).read(key, "2026-10-20") is None
        raise RuntimeError("undo")
    assert store.read(key, "2026-10-20") is not None
    with store.deleting_strategy(key.strategy_id):
        pass
    with pytest.raises(AccessError):
        store.save(key, {"as_of": "2026-10-21", "rows": []}, started_at=2)


def test_sql_http_strategy_delete_enforces_tenant_permission_and_cleans_history(env, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import screener, strategy
    from app.multitenancy import access

    token, account = register(env)
    foreign_token, _ = register(env)
    docs = TenantDocuments(env["db"], env["identity"].context(account, None, "http-cascade"))
    code = _strategy_code("custom_target")
    path = tmp_path / "target.py"
    path.write_text(code)
    docs.put("strategy_sources", "custom_target", {
        "source": "custom", "code": code, "definition": definition_payload(StrategyEngine._load_file(path)),
    })
    key = ResultIdentity("custom_target", "策略", "v1", "{}", "{}", "null", "stock", "1d")
    StrategyResults(docs).save(key, {"as_of": "2026-10-20", "rows": []}, started_at=1)
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.state.strategy_engine = StrategyEngine([])
    app.state.compute_executor = SimpleNamespace()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    app.include_router(strategy.router)
    app.include_router(screener.router)
    url = "/api/strategies/custom_target"
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", foreign_token)
        assert client.delete(url).status_code == 404
        client.cookies.set("tf_session", token)
        assert client.delete(url, headers={"Origin": "https://foreign.test"}).status_code == 403
        original = env["identity"].current_user
        monkeypatch.setattr(env["identity"], "current_user", lambda *a, **kw: {
            **original(*a, **kw), "permissions": ["business:read"],
        })
        assert client.delete(url).status_code == 403
        assert StrategyResults(docs).read(key, "2026-10-20") is not None
        monkeypatch.setattr(env["identity"], "current_user", original)
        assert client.delete(url).json() == {"ok": True, "warnings": []}
        assert client.get("/api/screener/result-history/custom_target").json()["records"] == []
        assert client.get(f"/api/screener/result-history/custom_target/{key.record_id('2026-10-20')}").status_code == 404


def test_sql_recreated_id_accepts_new_execution_and_rejects_old_snapshot(env, tmp_path):
    from app.services.strategy_results import capture_identity

    docs, engine = catalog(env, tmp_path)
    previous = docs.get("strategy_sources", "custom_target")
    key = capture_identity(engine, docs, "custom_target")
    old_task = engine.task("backtest", config={"strategy_id": key.strategy_id})
    engine.delete_source(key.strategy_id)
    engine.executor = SimpleNamespace(execute=lambda *a, **kw: {"definition": previous["definition"]})
    engine.save_source(key.strategy_id, previous["code"], "custom")
    current = capture_identity(engine, docs, key.strategy_id)
    assert key.fingerprint == current.fingerprint
    store = StrategyResults(docs)
    with pytest.raises(AccessError):
        store.save(key, {"as_of": "2026-10-20", "rows": []}, started_at=1)
    store.save(current, {"as_of": "2026-10-20", "rows": []}, started_at=2)
    jobs = ResearchJobs(docs)
    with pytest.raises(AccessError):
        jobs.submit(old_task, force=True)
    assert jobs.submit(engine.task("backtest", config={"strategy_id": key.strategy_id}))["state"] == "queued"


def test_sql_deleted_strategy_cannot_be_saved_as_a_new_candidate(env, tmp_path):
    docs, engine = catalog(env, tmp_path)
    engine.delete_source("custom_target")
    with pytest.raises(AccessError):
        SQLCandidateStore(docs).create(kind="strategy", name="旧候选", source_id="old-result",
                                        config={"strategy_id": "custom_target"}, metrics={}, data_as_of=None)
