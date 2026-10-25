from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_research import Compute, payload, tenant

from app.backtest.candidates import CandidateStore, CandidateValidationError
from app.cell.context import AccessError
from app.multitenancy.candidates import SQLCandidateStore
from app.multitenancy.research import ResearchWorker


def completed(env, jobs):
    row = jobs.submit(payload(jobs, public_release_id="original-release",
                              sources=[{"id": "private", "code": "original"}]))
    worker = ResearchWorker(env["db"], Compute({"research": {"runtime": {"build": "original"}}}))
    try:
        worker.run(jobs.context, worker.claim(jobs.context))
    finally:
        worker.close()
    return jobs.get(row["id"])


def test_replay_retains_private_inputs_and_reauthorizes_requester(env):
    from app.api.backtest import router

    token, user, jobs = tenant(env)
    original = completed(env, jobs)
    env["client"].app.include_router(router)
    env["client"].cookies.set("tf_session", token)
    response = env["client"].post("/api/backtest/jobs/" + original["id"] + "/replay", json={})
    assert response.status_code == 200 and response.json()["id"] != original["id"]
    replay = jobs.replay(original["id"])
    assert replay["id"] != original["id"]
    assert replay["input"]["sources"] == original["input"]["sources"]
    assert replay["input"]["public_release_id"] == "original-release"
    assert replay["input"]["expected_runtime"] == {"build": "original"}
    assert replay["input"]["replay_of"] == original["id"]
    _, _, other = tenant(env)
    with pytest.raises(AccessError) as denied:
        other.replay(original["id"])
    assert denied.value.status == 404
    env["control"].execute("UPDATE control.users SET active=false WHERE id=%s", (user["id"],))
    with pytest.raises(AccessError) as revoked:
        jobs.replay(original["id"])
    assert revoked.value.status == 403


def test_new_input_experiment_uses_current_engine_with_original_parameters(env):
    _, _, jobs = tenant(env)
    original = completed(env, jobs)
    calls = []
    def task(kind, config):
        calls.append((kind, deepcopy(config)))
        return payload(jobs, config=config, sources=[{"id": "private", "code": "new"}],
                       public_release_id="new-release")
    replay = jobs.replay(original["id"], new_inputs=True, engine=SimpleNamespace(task=task))
    assert calls == [("backtest", original["input"]["config"])]
    assert replay["input"]["public_release_id"] == "new-release"
    assert replay["input"]["sources"] != original["input"]["sources"]
    assert "expected_runtime" not in replay["input"]


def test_candidate_reference_protects_job_and_missing_reference_is_rejected(env):
    _, _, jobs = tenant(env)
    original = completed(env, jobs)
    store = SQLCandidateStore(jobs.documents)
    candidate = store.create(kind="strategy", name="retained", source_id="private",
                             config={"research_job_id": original["id"]}, metrics={}, data_as_of=None)
    with pytest.raises(AccessError) as referenced:
        jobs.delete(original["id"])
    assert referenced.value.status == 409
    store.delete(candidate["id"])
    jobs.delete(original["id"])
    with pytest.raises(AccessError):
        store.create(kind="strategy", name="missing", source_id="private",
                     config={"research_job_id": original["id"]}, metrics={}, data_as_of=None)


def test_active_and_legacy_tasks_cannot_claim_original_input_replay(env):
    _, _, jobs = tenant(env)
    row = jobs.submit(payload(jobs))
    with pytest.raises(AccessError) as active:
        jobs.replay(row["id"])
    assert active.value.status == 409
    jobs.cancel(row["id"])
    with pytest.raises(AccessError) as legacy:
        jobs.replay(row["id"])
    assert legacy.value.status == 409


def test_compute_refuses_changed_runtime_before_running(monkeypatch, tmp_path):
    from app.multitenancy import compute_worker

    monkeypatch.setattr("app.services.research_snapshot._runtime_identity", lambda: {"build": "new"})
    monkeypatch.setattr(compute_worker, "execute", lambda *_: pytest.fail("must not compute"))
    with pytest.raises(ValueError, match="运行构建已改变"):
        compute_worker.execute_request(
            {"kind": "backtest", "expected_runtime": {"build": "old"}}, tmp_path,
        )


def test_candidate_input_id_validation_and_roundtrip(tmp_path):
    store = CandidateStore(tmp_path)
    item = store.create(
        kind="strategy", name="fixed", source_id="s",
        config={"input_snapshot_id": "a" * 64, "strict_research": True}, metrics={}, data_as_of=None,
    )
    assert store.list()[0]["config"] == item["config"]
    with pytest.raises(CandidateValidationError):
        store.create(kind="strategy", name="bad", source_id="s",
                     config={"input_snapshot_id": "../escape"}, metrics={}, data_as_of=None)
