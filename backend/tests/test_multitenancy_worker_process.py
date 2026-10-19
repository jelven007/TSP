"""Per-process progress, stale files and bounded worker shutdown."""
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.multitenancy import worker_process
from app.multitenancy.health import WorkerHealth, readiness


def test_progress_requires_this_instance_and_successful_database_write(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_WORKER_HEARTBEAT_DIR", str(tmp_path))
    monkeypatch.setenv("TSP_WORKER_INSTANCE", "this-instance")
    failure = []

    @contextmanager
    def connection():
        if failure:
            raise RuntimeError("database unavailable")
        yield SimpleNamespace(execute=lambda *_: None)

    db = SimpleNamespace(control=SimpleNamespace(connection=connection),
                         settings=SimpleNamespace(id="full"))
    health = WorkerHealth(db, "research")
    health.record()
    value = json.loads((tmp_path / "progress.json").read_text())
    assert value["pid"] == os.getpid()
    assert value["instance"] == "this-instance"
    assert worker_process.progress(tmp_path, "this-instance", "research") == value["at"]
    assert worker_process.progress(tmp_path, "another", "research") is None
    assert worker_process.progress(tmp_path, "this-instance", "scheduled") is None
    health.last = 0
    failure.append(True)
    with pytest.raises(RuntimeError, match="database unavailable"):
        health.record()
    assert json.loads((tmp_path / "progress.json").read_text()) == value


def test_lease_renewal_does_not_hide_stalled_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_WORKER_HEARTBEAT_DIR", str(tmp_path))
    monkeypatch.setenv("TSP_WORKER_INSTANCE", "instance")

    @contextmanager
    def connection():
        yield SimpleNamespace(execute=lambda *_: None)

    health = WorkerHealth(SimpleNamespace(control=SimpleNamespace(connection=connection),
                                         settings=SimpleNamespace(id="full")), "scheduled")
    health.record(progress=False)
    assert not (tmp_path / "progress.json").exists()
    # A recent renewal must not throttle the dispatch loop's own proof of progress.
    health.record()
    assert (tmp_path / "progress.json").exists()
    health.record(False)
    assert worker_process.progress(tmp_path, "instance", "scheduled") is None


@pytest.mark.parametrize("payload", ["broken", "[]", '{"at":NaN}', '{"at":1e999}'])
def test_invalid_progress_fails_closed(tmp_path, payload):
    (tmp_path / "progress.json").write_text(payload)
    assert worker_process.progress(tmp_path, "instance", "research") is None


@pytest.mark.parametrize(("kind", "fresh_age", "stale_age"), [
    ("research", 119, 121),
    ("realtime", 419, 421),
    ("notifications", 119, 121),
    ("scheduled", 659, 661),
])
def test_database_and_process_health_share_kind_specific_freshness(kind, fresh_age, stale_age):
    from app.multitenancy.health import worker_fresh

    assert worker_fresh(kind, True, fresh_age)
    assert not worker_fresh(kind, True, stale_age)
    assert not worker_fresh(kind, False, fresh_age)
    assert not worker_fresh("unknown", True, 0)


def test_api_readiness_uses_the_same_kind_specific_worker_ages(tmp_path):
    rows = [
        {"kind": "research", "healthy": True, "age_seconds": 100},
        {"kind": "realtime", "healthy": True, "age_seconds": 300},
        {"kind": "notifications", "healthy": True, "age_seconds": 100},
        {"kind": "scheduled", "healthy": True, "age_seconds": 600},
    ]

    @contextmanager
    def control_connection():
        yield SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: rows))

    @contextmanager
    def data_connection():
        yield SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: {"?column?": 1}))

    db = SimpleNamespace(
        settings=SimpleNamespace(id="full"),
        control=SimpleNamespace(connection=control_connection),
        data=SimpleNamespace(connection=data_connection),
    )
    (tmp_path / ".public_release.json").write_text('{"id":"release-1"}')
    state = SimpleNamespace(
        account_identity=SimpleNamespace(db=db),
        quote_service=SimpleNamespace(
            snapshot={"platform_ready": True},
            received_at=time.monotonic(),
            bridge_status=lambda: {"connected": True},
        ),
        indicators_ready=True,
        compute_executor=SimpleNamespace(health=lambda: {"ready": True}),
    )

    result = readiness(state, tmp_path)
    assert result["ready"]
    assert result["checks"]["realtime_worker"]["max_age_seconds"] == 420
    assert result["checks"]["scheduled_worker"]["max_age_seconds"] == 660


def test_stalled_child_is_terminated_even_if_old_heartbeat_exists(tmp_path):
    worker_process.write_json(tmp_path / "progress.json", {
        "pid": os.getpid(), "instance": "old", "kind": "research",
        "at": time.monotonic(), "healthy": True,
    })
    started = time.monotonic()
    result = worker_process.supervise(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        kind="research", directory=tmp_path, max_age=.2, startup=.2, grace=.1, poll=.02,
    )
    assert result == 1
    assert time.monotonic() - started < 5
    assert not worker_process.check(tmp_path, "research", max_age=120)


def test_child_failure_propagates(tmp_path):
    result = worker_process.supervise(
        [sys.executable, "-c", "raise SystemExit(7)"],
        kind="research", directory=tmp_path, max_age=30, startup=30, grace=.1, poll=.02,
    )
    assert result == 7


@pytest.mark.skipif(os.name == "nt", reason="Compose supervisor uses POSIX signals")
def test_supervisor_forwards_stop_and_reaps_child(tmp_path):
    directory = tmp_path / "health"
    code = (
        "from pathlib import Path; from app.multitenancy.worker_process import supervise; "
        f"supervise([{sys.executable!r}, '-c', 'import time; time.sleep(30)'], "
        f"kind='research', directory=Path({str(directory)!r}), max_age=30, startup=30, grace=.2)"
    )
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 5
        while not (directory / "owner.json").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        owner = json.loads((directory / "owner.json").read_text())
        child.terminate()
        assert child.wait(timeout=5) == 0
        with pytest.raises(ProcessLookupError):
            os.kill(owner["pid"], 0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
