"""Disk failures must not acknowledge lost jobs or rewrite completed work."""
from __future__ import annotations

import json

import pytest

from app.services import pipeline_jobs
from app.services.pipeline_jobs import JobStore


def _unavailable(*_args):
    raise PermissionError("injected replacement failure")


def test_failed_create_does_not_accept_or_reserve_a_job(tmp_path, monkeypatch):
    store = JobStore(store_dir=tmp_path)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(pipeline_jobs.os, "replace", _unavailable)
        with pytest.raises(RuntimeError, match="持久化"):
            store.create(timeout_s=60)
    assert store.active_id() is None
    assert store.list_recent() == []
    assert list(tmp_path.iterdir()) == []
    job_id, created = store.create(timeout_s=60)
    assert created
    assert store.get(job_id)["status"] == "queued"


def test_failed_start_preserves_the_accepted_queued_state(tmp_path, monkeypatch):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(timeout_s=60)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(pipeline_jobs.os, "replace", _unavailable)
        with pytest.raises(RuntimeError, match="持久化"):
            store.start(job_id)
        assert store.get(job_id)["status"] == "queued"
        assert store.get(job_id)["started_at"] is None
    assert json.loads((tmp_path / f"{job_id}.json").read_text())["status"] == "queued"
    store.start(job_id)
    assert store.get(job_id)["status"] == "running"


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_terminal_failure_retains_the_real_result_and_blocks_new_jobs(
    tmp_path, monkeypatch, terminal,
):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(timeout_s=60)
    store.start(job_id)
    if terminal == "cancelled":
        store.terminate(job_id, "test cancellation")
    with monkeypatch.context() as unavailable:
        unavailable.setattr(pipeline_jobs.os, "replace", _unavailable)
        with pytest.raises(RuntimeError, match="持久化"):
            if terminal == "succeeded":
                store.succeed(job_id, {"rows": 12})
            elif terminal == "failed":
                store.fail(job_id, "provider unavailable")
            else:
                store.cancelled(job_id)
        record = store.get(job_id)
        assert record["status"] == terminal
        assert record["persistence_pending"] is True
        if terminal == "succeeded":
            assert record["result"] == {"rows": 12}
            store.fail(job_id, "the wrapper must not replace a completed result")
            assert store.get(job_id)["status"] == "succeeded"
        with pytest.raises(RuntimeError, match="持久化"):
            store.create(timeout_s=60)
        assert len(store.list_recent()) == 1
    assert store.get(job_id)["status"] == terminal
    assert not store.get(job_id).get("persistence_pending", False)
    assert store.active_id() is None
    assert store.create(timeout_s=60)[1]


def test_restart_recovers_fsynced_terminal_record_after_replacement_failure(tmp_path, monkeypatch):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(timeout_s=60)
    store.start(job_id)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(pipeline_jobs.os, "replace", _unavailable)
        with pytest.raises(RuntimeError, match="持久化"):
            store.succeed(job_id, {"rows": 12})
    recovered = JobStore(store_dir=tmp_path)
    record = recovered.get(job_id)
    assert record["status"] == "succeeded"
    assert record["result"] == {"rows": 12}
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.pending"))


def test_failed_create_keeps_previously_retained_terminal_history(tmp_path, monkeypatch):
    store = JobStore(max_jobs=1, store_dir=tmp_path)
    old_id, _ = store.create(timeout_s=60)
    store.start(old_id)
    store.succeed(old_id, {"rows": 3})
    with monkeypatch.context() as unavailable:
        unavailable.setattr(pipeline_jobs.os, "replace", _unavailable)
        with pytest.raises(RuntimeError, match="持久化"):
            store.create(timeout_s=60)
    assert store.get(old_id)["status"] == "succeeded"


def test_unflushed_partial_output_is_not_recovered_as_a_terminal(tmp_path, monkeypatch):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(timeout_s=60)
    store.start(job_id)

    def failed_flush(_descriptor):
        raise OSError("injected fsync failure")

    with monkeypatch.context() as unavailable:
        unavailable.setattr(pipeline_jobs.os, "fsync", failed_flush)
        with pytest.raises(RuntimeError, match="持久化"):
            store.succeed(job_id, {"rows": 12})
        assert not list(tmp_path.glob("*.tmp"))
    assert JobStore(store_dir=tmp_path).get(job_id)["status"] == "interrupted"


def test_repeated_replacement_failure_preserves_the_fsynced_terminal_file(tmp_path, monkeypatch):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(timeout_s=60)
    store.start(job_id)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(pipeline_jobs.os, "replace", _unavailable)
        with pytest.raises(RuntimeError, match="持久化"):
            store.succeed(job_id, {"rows": 12})
        journal = next(tmp_path.glob(f"{job_id}.json.*.pending"))
        before = journal.read_bytes()
        assert store.get(job_id)["persistence_pending"]
        assert journal.read_bytes() == before
    assert JobStore(store_dir=tmp_path).get(job_id)["status"] == "succeeded"


def test_failed_start_then_failure_recovers_terminal_instead_of_older_running_journal(
    tmp_path, monkeypatch,
):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(timeout_s=60)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(pipeline_jobs.os, "replace", _unavailable)
        with pytest.raises(RuntimeError, match="持久化"):
            store.start(job_id)
        with pytest.raises(RuntimeError, match="持久化"):
            store.fail(job_id, "execution never started")

    recovered = JobStore(store_dir=tmp_path)

    assert recovered.get(job_id)["status"] == "failed"
    assert recovered.get(job_id)["error"] == "execution never started"
    assert not list(tmp_path.glob("*.pending"))


def test_restart_ignores_a_complete_json_file_without_the_fsync_journal_marker(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(timeout_s=60)
    store.start(job_id)
    candidate = {
        **store.get(job_id), "status": "succeeded",
        "result": {"rows": 12}, "finished_at": "2026-10-25T01:00:00+00:00",
    }
    (tmp_path / f"{job_id}.json.tmp").write_text(json.dumps(candidate))

    recovered = JobStore(store_dir=tmp_path)

    assert recovered.get(job_id)["status"] == "interrupted"


def test_non_finite_result_does_not_leave_an_unserializable_success(tmp_path):
    store = JobStore(store_dir=tmp_path)
    job_id, _ = store.create(timeout_s=60)
    store.start(job_id)

    with pytest.raises(ValueError):
        store.succeed(job_id, {"rows": float("nan")})

    assert store.get(job_id)["status"] == "running"
    store.fail(job_id, "invalid output")
    assert store.get(job_id)["status"] == "failed"
    assert JobStore(store_dir=tmp_path).get(job_id)["status"] == "failed"


def test_pipeline_returns_503_without_scheduling_unpersisted_work(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import pipeline
    from app.main import job_persistence_error_handler
    from app.services import pipeline_runner

    app = FastAPI(exception_handlers={
        pipeline_jobs.JobPersistenceError: job_persistence_error_handler,
    })
    app.include_router(pipeline.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.capabilities = object()
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_runner, "job_store", store)
    before = dict(pipeline_runner._background_tasks)
    monkeypatch.setattr(pipeline_jobs.os, "replace", _unavailable)

    response = TestClient(app).post("/api/pipeline/run")

    assert response.status_code == 503
    assert response.json()["code"] == "job_state_persistence_unavailable"
    assert pipeline_runner._background_tasks == before
    assert store.active_id() is None
    assert str(tmp_path) not in response.text
