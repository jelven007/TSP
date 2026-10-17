from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.market_time import CN_TZ
from scripts.observe_candidate_soak import Observer, arm, parse_endpoints

START = datetime(2026, 10, 17, 16, 0, tzinfo=CN_TZ)


def test_passive_soak_observer_keeps_load_and_duration_certification_separate(
    tmp_path,
    monkeypatch,
):
    run_dir = tmp_path / "soak"
    arm(
        run_dir,
        duration_hours=0.01,
        interval_seconds=5,
        endpoints={"api": "http://127.0.0.1:3018/health/ready"},
        containers=["candidate-api"],
        disk_paths=[tmp_path],
        now=START,
    )
    container = {
        "candidate-api": {
            "ok": True,
            "status": "running",
            "started_at": "2026-10-17T00:00:00Z",
            "finished_at": "0001-01-01T00:00:00Z",
            "exit_code": 0,
            "restart_count": 0,
            "image_id": "sha256:candidate",
        }
    }
    monkeypatch.setattr(
        "scripts.observe_candidate_soak.probe",
        lambda _url: {
            "ok": True,
            "status": "ok",
            "ready": True,
            "status_code": 200,
            "latency_ms": 2.5,
            "version": "0.4.2",
            "checks": {},
        },
    )
    monkeypatch.setattr(
        "scripts.observe_candidate_soak.container_snapshot",
        lambda _names: container,
    )
    monkeypatch.setattr(
        "scripts.observe_candidate_soak.disk_snapshot",
        lambda _paths: {
            str(tmp_path): {
                "ok": True,
                "free_bytes": 10 * 1024**3,
                "total_bytes": 20 * 1024**3,
            }
        },
    )

    observer = Observer(run_dir)
    for seconds in (0, 5, 10, 15, 20, 25, 30, 35, 36):
        observer.step(START + timedelta(seconds=seconds))

    report = json.loads((run_dir / "report.json").read_text())
    assert report["status"] == "succeeded"
    assert report["checks"] == {
        "all_endpoints_ready": True,
        "container_identity_stable": True,
        "disk_headroom": True,
        "full_duration_observed": True,
        "sampling_continuous": True,
    }
    assert report["certification"] == {
        "active_100_user_load": False,
        "complete_trading_day": False,
        "passive_72h_observation": False,
    }


def test_endpoint_declarations_are_named_unique_urls():
    assert parse_endpoints(
        [
            "api=http://127.0.0.1:3018/health/ready",
            "gateway=http://127.0.0.1:3031/health/ready",
        ]
    ) == {
        "api": "http://127.0.0.1:3018/health/ready",
        "gateway": "http://127.0.0.1:3031/health/ready",
    }
