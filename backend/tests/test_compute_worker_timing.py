"""Worker timings stay outside financial payloads and retain failure evidence."""
import io
import json
from types import SimpleNamespace

import pytest

from app.multitenancy import compute_worker as worker


@pytest.mark.parametrize("kind,failed", [("health", False), ("plan", False), ("plan", True)])
def test_worker_terminal_event_contains_separate_phase_timings(monkeypatch, kind, failed):
    stream = io.StringIO()
    monkeypatch.setattr(worker, "PROTOCOL", stream)
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(
        buffer=io.BytesIO(json.dumps({"kind": kind}).encode() + b"\n"),
    ))

    def execute(*_):
        if failed:
            raise ValueError("fixture failure")
        return {"ready": True} if kind == "health" else {"history_bars": 20, "nullable": None}

    monkeypatch.setattr(worker, "execute_request", execute)
    worker.main()
    message = json.loads(stream.getvalue())
    assert message["type"] == ("error" if failed else "result")
    timing = message.get("worker_timing")
    assert timing is not None
    for key in ("input_ms", "imports_ms", "registry_ms", "execute_ms", "total_ms"):
        assert 0 <= timing[key] <= timing["total_ms"] + .01
    assert abs(sum(timing[key] for key in ("input_ms", "imports_ms", "registry_ms", "execute_ms"))
               - timing["total_ms"]) < 50
    if not failed:
        assert message["payload"] == execute()
    else:
        assert message["message"] == "fixture failure"
