"""Metrics must be emitted by the actual worker, including its reset identity."""
import json
import os
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.multitenancy import worker_process
from app.services import observability


def test_progress_carries_current_process_stage_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_WORKER_HEARTBEAT_DIR", str(tmp_path))
    monkeypatch.setenv("TSP_WORKER_INSTANCE", "owned-worker")
    before = observability.runtime_observability_snapshot()
    observability.observe_runtime_duration("monitor.compute", 37)
    worker_process.record("realtime", True)
    value = json.loads((tmp_path / "progress.json").read_text())
    assert value["pid"] == os.getpid()
    assert value["instance"] == "owned-worker"
    assert "observability" in value
    metrics = value["observability"]
    assert metrics["process"]["instance_id"] == before["process"]["instance_id"]
    assert metrics["stages"]["monitor.compute"]["calls"] == (
        before["stages"].get("monitor.compute", {}).get("calls", 0) + 1
    )
    assert datetime.fromisoformat(value["captured_at"]).utcoffset() is not None
    assert worker_process.progress(tmp_path, "owned-worker", "realtime") == value["at"]


def test_platform_metrics_use_actual_process_and_require_market_authorization(monkeypatch):
    from app.cell import platform_market

    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "test-observer-token")
    with pytest.raises(HTTPException) as denied:
        platform_market.observability(authorization="Bearer wrong")
    assert denied.value.status_code == 401
    before = observability.runtime_observability_snapshot()
    observability.observe_runtime_duration("monitor.compute", 5)
    value = platform_market.observability(authorization="Bearer test-observer-token")
    assert value["runtime_process"]["instance_id"] == before["process"]["instance_id"]
    assert value["stages"]["monitor.compute"]["calls"] >= 1
    assert "routes" not in value and "pools" not in value
