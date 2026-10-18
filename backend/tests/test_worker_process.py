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
from app.multitenancy.health import WorkerHealth


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
    assert worker_process.progress(tmp_path, os.getpid(), "this-instance", "research") == value["at"]
    assert worker_process.progress(tmp_path, os.getpid(), "another", "research") is None
    assert worker_process.progress(tmp_path, os.getpid() + 1, "this-instance", "research") is None
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
    assert worker_process.progress(tmp_path, os.getpid(), "instance", "scheduled") is None


@pytest.mark.parametrize("payload", ["broken", "[]", '{"at":NaN}', '{"at":1e999}'])
def test_invalid_progress_fails_closed(tmp_path, payload):
    (tmp_path / "progress.json").write_text(payload)
    assert worker_process.progress(tmp_path, 1, "instance", "research") is None


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
