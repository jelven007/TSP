"""Broker timings separate host spawn, time to output, process lifetime and cleanup."""
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from app.multitenancy.compute_broker import ComputeBroker
from app.multitenancy.executor import ComputeExecutor
from app.services.observability import observe_runtime_duration, runtime_observability_snapshot


def test_broker_exports_own_stage_metrics_without_launching_sandbox(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    with tempfile.TemporaryDirectory(prefix="tsp-metrics-", dir="/tmp") as directory:
        root = Path(directory)
        with ComputeBroker(root / "c.sock", "pinned", root, 1) as broker:
            monkeypatch.setattr(broker, "command", lambda *_: (_ for _ in ()).throw(
                AssertionError("metrics must not launch compute")))
            thread = threading.Thread(target=broker.serve_forever, daemon=True)
            thread.start()
            try:
                observe_runtime_duration("compute.cleanup", 7)
                expected = runtime_observability_snapshot()
                import socket

                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(root / "c.sock"))
                    client.sendall(b'{"kind":"observability"}\n')
                    with client.makefile("rb") as stream:
                        result = json.loads(stream.readline())
                assert result["type"] == "result"
                assert result["payload"]["process"]["instance_id"] == expected["process"]["instance_id"]
                assert result["payload"]["stages"] == expected["stages"]
            finally:
                broker.shutdown()
                thread.join(2)


def test_monitor_phase_timings_preserve_cleanup_and_host_ownership(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    # A sandbox's timing payload must not replace host measurements.
    message = json.dumps({"type": "result", "payload": {"_compute_timing": {"spawn_ms": -1}}})
    with tempfile.TemporaryDirectory(prefix="tsp-phase-", dir="/tmp") as directory:
        root = Path(directory)
        with ComputeBroker(root / "c.sock", "pinned", root, 1) as broker:
            monkeypatch.setattr(broker, "command", lambda *_: [
                sys.executable, "-c", f"import sys; sys.stdin.readline(); print({message!r})",
            ])
            thread = threading.Thread(target=broker.serve_forever, daemon=True)
            thread.start()
            try:
                result = ComputeExecutor(str(root / "c.sock")).execute({
                    "kind": "monitor", "tenant_id": "fixture",
                })
                timing = result["_compute_timing"]
                for key in ("spawn_ms", "first_output_ms", "process_ms", "cleanup_ms"):
                    assert key in timing
                    assert 0 <= timing[key] <= timing["sandbox_ms"]
                assert timing["spawn_ms"] <= timing["first_output_ms"] <= timing["process_ms"]
                assert abs(timing["process_ms"] + timing["cleanup_ms"] - timing["sandbox_ms"]) < 5
                assert broker.permits.usage()["active"] == 0
            finally:
                broker.shutdown()
                thread.join(2)
