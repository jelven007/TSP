"""Strategy deletion owns all snapshots, rather than one date or display name."""
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_strategy_delete import _request, _strategy_code

from app.api.strategy import StrategyCodeSaveRequest, delete_strategy, save_strategy_code
from app.cell.context import AccessError
from app.services import preferences, strategy_results
from app.services.data_release import persist_backtest_manifest
from app.strategy.engine import StrategyEngine


@pytest.fixture
def setup(tmp_path, monkeypatch):
    directory = tmp_path / "strategies" / "custom"
    directory.mkdir(parents=True)
    path = directory / "custom_target.py"
    path.write_text(_strategy_code("custom_target"))
    (directory / "other.py").write_text(_strategy_code("custom_other"))
    engine = StrategyEngine([directory])
    monkeypatch.setattr(preferences, "get_strategy_monitor_ids", lambda: [])
    request = _request(tmp_path, engine)
    store = strategy_results.StrategyResults(tmp_path)
    key = strategy_results.capture_identity(engine, tmp_path, "custom_target")
    return tmp_path, path, engine, request, store, key


def seed(store, key):
    variants = [key, replace(key, params_json='{"threshold":2}'),
                replace(key, strategy_name="重命名", strategy_version="v2", timeframe="1m")]
    for variant in variants:
        for day in ("2026-10-20", "2026-10-21"):
            store.save(variant, {"as_of": day, "rows": [{"symbol": "A"}]}, started_at=1)
            store.read(variant, day)  # Exercise the hot cache as well.
    other = replace(key, strategy_id="custom_other")
    store.save(other, {"as_of": "2026-10-20", "rows": [{"symbol": "B"}]}, started_at=1)
    return other


def test_api_deletes_all_history_versions_and_preserves_other_strategy(setup):
    _, path, engine, request, store, key = setup
    other = seed(store, key)
    assert len(store.list_history(key.strategy_id)["records"]) == 6
    assert delete_strategy(key.strategy_id, request)["ok"]
    assert not path.exists() and not engine.has(key.strategy_id)
    assert store.list_history(key.strategy_id)["records"] == []
    assert store.read(key, "2026-10-20") is None
    assert store.read(other, "2026-10-20")["result"]["rows"] == [{"symbol": "B"}]


def test_source_delete_failure_preserves_history_and_write_eligibility(setup, monkeypatch):
    _, path, engine, request, store, key = setup
    seed(store, key)
    original = Path.unlink

    def blocked(target, *args, **kwargs):
        if target == path:
            raise PermissionError(30, "Read-only file system")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked)
    with pytest.raises(HTTPException) as error:
        delete_strategy(key.strategy_id, request)
    assert error.value.status_code == 409
    assert path.exists() and engine.has(key.strategy_id)
    assert len(store.list_history(key.strategy_id)["records"]) == 6
    store.save(key, {"as_of": "2026-10-22", "rows": []}, started_at=2)


def test_late_run_cannot_recreate_history_after_delete_or_same_id_recreation(setup):
    root, _, engine, request, store, key = setup
    seed(store, key)
    delete_strategy(key.strategy_id, request)
    with pytest.raises(AccessError) as error:
        store.save(key, {"as_of": "2026-10-22", "rows": []}, started_at=10)
    assert error.value.status == 409
    save_strategy_code(StrategyCodeSaveRequest(
        strategy_id=key.strategy_id, code=_strategy_code(key.strategy_id),
        target_source="custom", mode="create",
    ), request)
    current = strategy_results.capture_identity(engine, root, key.strategy_id)
    assert current.fingerprint == key.fingerprint  # Generation is not a trading parameter.
    with pytest.raises(AccessError):
        store.save(key, {"as_of": "2026-10-22", "rows": [{"symbol": "OLD"}]}, started_at=20)
    store.save(current, {"as_of": "2026-10-22", "rows": [{"symbol": "NEW"}]}, started_at=30)
    assert store.read(current, "2026-10-22")["result"]["rows"] == [{"symbol": "NEW"}]


def test_history_storage_failure_keeps_strategy_source(setup, monkeypatch):
    _, path, engine, request, store, key = setup
    seed(store, key)
    original = store._local

    from contextlib import contextmanager

    @contextmanager
    def failing(self, *, write=False):
        with original(write=write) as conn:
            if write:
                raise OSError("history unavailable")
            yield conn

    monkeypatch.setattr(strategy_results.StrategyResults, "_local", failing)
    with pytest.raises(HTTPException) as error:
        delete_strategy(key.strategy_id, request)
    assert error.value.status_code == 503
    assert path.exists() and engine.has(key.strategy_id)
    assert len(store.list_history(key.strategy_id)["records"]) == 6


def test_local_backtest_manifests_candidates_and_reconnect_results_are_removed(setup):
    from app.api import backtest
    from app.backtest.candidates import CandidateStore

    root, _, _, request, store, key = setup
    seed(store, key)
    for sid, run in ((key.strategy_id, "target-run"), ("custom_other", "other-run")):
        persist_backtest_manifest(root, kind="strategy", result={
            "run_id": run, "config": {"strategy_id": sid}, "provenance": {}, "trades": [],
        })
        CandidateStore(root).create(kind="strategy", name=sid, source_id=run,
                                    config={"strategy_id": sid}, metrics={}, data_as_of=None)
    import threading
    from types import SimpleNamespace
    job = SimpleNamespace(key="cascade-test", strategy_id=key.strategy_id,
                          cancel_event=threading.Event(), result={"stats": {}}, error=None,
                          done=False, finish_ts=0)
    with backtest._jobs_lock:
        backtest._running_jobs[job.key] = job
    try:
        delete_strategy(key.strategy_id, request)
        assert not (root / "backtest_results/manifests/target-run.json").exists()
        assert (root / "backtest_results/manifests/other-run.json").exists()
        assert [item["config"]["strategy_id"] for item in CandidateStore(root).list()] == ["custom_other"]
        assert job.cancel_event.is_set()
        assert job.key not in backtest._running_jobs
        backtest._finish_job(job, result={"stats": {"old": True}})
        assert job.result is None
    finally:
        backtest._running_jobs.pop(job.key, None)


def test_local_research_cleanup_failure_restores_files_and_history(setup, monkeypatch):
    root, path, engine, request, store, key = setup
    seed(store, key)
    persist_backtest_manifest(root, kind="strategy", result={
        "run_id": "target-run", "config": {"strategy_id": key.strategy_id}, "provenance": {},
    })
    manifest = root / "backtest_results/manifests/target-run.json"
    original = Path.unlink

    def blocked(target, *args, **kwargs):
        if target == manifest:
            raise PermissionError("result cannot be removed")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked)
    with pytest.raises(HTTPException):
        delete_strategy(key.strategy_id, request)
    assert path.exists() and manifest.exists() and engine.has(key.strategy_id)
    assert len(store.list_history(key.strategy_id)["records"]) == 6


def test_deleted_strategy_rejects_late_backtest_manifest(setup):
    root, _, _, request, _, key = setup
    delete_strategy(key.strategy_id, request)
    with pytest.raises(AccessError):
        persist_backtest_manifest(root, kind="strategy", result={
            "run_id": "late", "config": {"strategy_id": key.strategy_id}, "provenance": {},
        })
    assert not (root / "backtest_results/manifests/late.json").exists()


def test_late_legacy_cache_write_does_not_restore_deleted_strategy(setup):
    from app.services import strategy_cache

    root, _, _, request, _, key = setup
    delete_strategy(key.strategy_id, request)
    strategy_cache.write_cache(root, "2026-10-20", {
        key.strategy_id: {"rows": [{"symbol": "OLD"}]},
        "custom_other": {"rows": [{"symbol": "B"}]},
    })
    assert key.strategy_id not in strategy_cache.read_cache(root)["results"]
    assert "custom_other" in strategy_cache.read_cache(root)["results"]


def test_sqlite_commit_failure_restores_source_and_result_files(setup, monkeypatch):
    from contextlib import contextmanager

    root, path, engine, request, store, key = setup
    seed(store, key)
    persist_backtest_manifest(root, kind="strategy", result={
        "run_id": "target-run", "config": {"strategy_id": key.strategy_id}, "provenance": {},
    })
    original = strategy_results.StrategyResults._local

    @contextmanager
    def failing(self, *, write=False):
        with original(self, write=write) as conn:
            yield conn
            if write:
                raise OSError("commit failed")

    monkeypatch.setattr(strategy_results.StrategyResults, "_local", failing)
    with pytest.raises(HTTPException) as error:
        delete_strategy(key.strategy_id, request)
    assert error.value.status_code == 503
    assert path.exists() and engine.has(key.strategy_id)
    assert (root / "backtest_results/manifests/target-run.json").exists()
    assert len(store.list_history(key.strategy_id)["records"]) == 6


def test_linked_result_directory_is_rejected_without_deleting_external_files(setup, tmp_path):
    root, path, engine, request, store, key = setup
    seed(store, key)
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "outside.json"
    external.write_text('{"config":{"strategy_id":"custom_target"}}')
    (root / "backtest_results").mkdir()
    (root / "backtest_results/manifests").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException):
        delete_strategy(key.strategy_id, request)
    assert external.exists() and path.exists() and engine.has(key.strategy_id)
    assert len(store.list_history(key.strategy_id)["records"]) == 6


def test_same_id_recreation_rejects_old_manifest_and_cache_generation(setup):
    from app.services import strategy_cache

    root, _, engine, request, store, key = setup
    delete_strategy(key.strategy_id, request)
    save_strategy_code(StrategyCodeSaveRequest(
        strategy_id=key.strategy_id, code=_strategy_code(key.strategy_id),
        target_source="custom", mode="create",
    ), request)
    current = strategy_results.capture_identity(engine, root, key.strategy_id)
    with pytest.raises(AccessError):
        persist_backtest_manifest(root, kind="strategy", result={
            "run_id": "old", "config": {"strategy_id": key.strategy_id}, "provenance": {},
        }, strategy_generation=key.storage_generation)
    strategy_cache.write_cache(root, "2026-10-20", {key.strategy_id: {"rows": [{"symbol": "OLD"}]}},
                               generations={key.strategy_id: key.storage_generation})
    assert key.strategy_id not in strategy_cache.read_cache(root)["results"]
    store.save(current, {"as_of": "2026-10-20", "rows": []}, started_at=2)


def test_delayed_identity_capture_keeps_the_execution_snapshot_generation(setup):
    root, _, engine, request, store, key = setup
    frozen = strategy_results.execution_snapshot(engine, root)
    delete_strategy(key.strategy_id, request)
    save_strategy_code(StrategyCodeSaveRequest(
        strategy_id=key.strategy_id, code=_strategy_code(key.strategy_id),
        target_source="custom", mode="create",
    ), request)
    late = strategy_results.capture_identity(frozen, root, key.strategy_id)
    with pytest.raises(AccessError):
        store.save(late, {"as_of": "2026-10-20", "rows": []}, started_at=1)


def test_deleted_strategy_cannot_be_saved_as_a_new_local_candidate(setup):
    from app.backtest.candidates import CandidateStore

    root, _, _, request, _, key = setup
    delete_strategy(key.strategy_id, request)
    with pytest.raises(AccessError):
        CandidateStore(root).create(kind="strategy", name="旧候选", source_id="old-result",
                                    config={"strategy_id": key.strategy_id}, metrics={}, data_as_of=None)
