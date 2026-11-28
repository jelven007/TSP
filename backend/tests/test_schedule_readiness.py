import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import pipeline
from app.services import pipeline_jobs, post_close_integrity, preferences


@pytest.fixture
def system(tmp_path, monkeypatch):
    clock = ["2026-11-28T06:53:00+08:00"]
    auto = [True]
    monkeypatch.setattr(post_close_integrity, "cn_now", lambda: datetime.fromisoformat(clock[0]))
    monkeypatch.setattr(pipeline, "cn_now", lambda: datetime.fromisoformat(clock[0]), raising=False)
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: auto[0])
    monkeypatch.setattr(pipeline, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "jobs"))
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(pipeline.router)

    def save(path, value):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(value))
        return file

    return TestClient(app), save, clock, auto


def read(client):
    response = client.get("/api/pipeline/jobs?limit=1&include_readiness=true")
    assert response.status_code == 200
    return response.json()["schedule_readiness"]


def test_weekend_shows_failed_announcement_check_beside_passed_close_review(system):
    client, save, _, _ = system
    evening = save("user_data/related_market_data_sync.json", {
        "maintenance_date": "2026-11-27", "maintenance_status": "partial",
        "datasets": {"announcements": {"status": "success"}},
        "stock_status": {
            "target_date": "2026-11-30", "scheduled_for": "2026-11-27T20:30:00+08:00",
            "cutoff": "2026-11-27T20:30:00+08:00", "observed_at": "2026-11-27T20:30:11+08:00",
            "status": "failed", "evidence_policy": "stock_status_v2", "coverage_complete": False,
            "universe_count": 5224, "covered_count": 0, "pending_count": 5224,
            "errors": {"SZ": "opaque provider detail"},
        },
    })
    save("post_close_sync/current.json", {
        "day": "2026-11-27", "status": "failed", "daily_settlement_status": "succeeded",
        "daily_settlement_release_id": "settlement-27",
        "periods": {"1m": {"remaining": 1200}},
    })
    save("post_close_integrity/current.json", {
        "day": "2026-11-27", "status": "succeeded", "last_finished_at": "2026-11-27T23:53:50+08:00",
        "result": {"close_review": {"after": {"checked_count": 5219, "valid_count": 5219, "invalid_count": 0}},
                   "gap_check": {"remaining": 0, "suspended_skipped": 5}},
    })
    before = evening.read_bytes()
    result = read(client)
    assert result["stock_status"]["target_date"] == "2026-11-30"
    assert result["stock_status"]["coverage"] == "unverified"
    assert result["stock_status"]["counts"]["pending"] == 5224
    assert result["post_close"]["trade_date"] == "2026-11-27"
    assert result["post_close"]["sync"]["status"] == "failed"
    assert result["post_close"]["daily"]["status"] == "succeeded"
    assert result["post_close"]["review"]["coverage"] == "complete"
    assert result["post_close"]["review"]["remaining"] == 0
    assert result["next_check"]["kind"] == "evening_status"
    assert "opaque provider detail" not in json.dumps(result)
    assert evening.read_bytes() == before
    assert "schedule_readiness" not in client.get("/api/pipeline/jobs?limit=1").json()
    updated = json.loads(before)
    updated["stock_status"].update(covered_count=2314, pending_count=2910)
    replacement = evening.with_suffix(".tmp")
    replacement.write_text(json.dumps(updated))
    replacement.replace(evening)
    assert read(client)["stock_status"]["coverage"] == "partial"
    assert read(client)["stock_status"]["counts"]["pending"] == 2910


def test_second_morning_round_does_not_reuse_first_and_preserves_deadline(system):
    client, save, clock, _ = system
    clock[0] = "2026-11-30T09:12:00+08:00"
    morning = {
        "target_date": "2026-11-30", "scheduled_for": "2026-11-30T08:30:00+08:00",
        "cutoff": "2026-11-30T08:31:00+08:00", "finished_at": "2026-11-30T08:32:00+08:00",
        "status": "succeeded", "coverage_complete": True, "evidence_policy": "stock_status_v2",
    }
    save("instrument_status/morning_check.json", morning)
    result = read(client)["stock_status"]
    assert result["scheduled_for"] == "2026-11-30T09:00:00+08:00"
    assert result["status"] == "not_run" and result["coverage"] == "unverified"
    save("instrument_status/morning_check.json", {
        **morning, "scheduled_for": "2026-11-30T09:00:00+08:00",
        "cutoff": "2026-11-30T09:03:00+08:00", "finished_at": "2026-11-30T09:05:00+08:00",
        "publish_pending": True,
    })
    assert read(client)["stock_status"]["coverage"] == "pending_publication"
    verified = {
        **morning, "scheduled_for": "2026-11-30T09:00:00+08:00",
        "cutoff": "2026-11-30T09:03:00+08:00", "finished_at": "2026-11-30T09:05:00+08:00",
    }
    save("instrument_status/morning_check.json", {**verified, "evidence_policy": None})
    assert read(client)["stock_status"]["coverage"] == "unverified"
    save("instrument_status/morning_check.json", verified)
    assert read(client)["stock_status"]["coverage"] == "complete"
    clock[0] = "2026-11-30T09:30:00+08:00"
    assert read(client)["stock_status"]["deadline_elapsed"] is True


def test_missing_calendar_pause_and_corrupt_file_do_not_invent_readiness(system):
    client, save, clock, auto = system
    auto[0] = False
    clock[0] = "2026-12-31T17:00:00+08:00"
    save("post_close_integrity/current.json", {"day": "2026-12-30", "status": "succeeded"})
    save("post_close_sync/current.json", {}).write_text("broken")
    result = read(client)
    assert result["automatic_enabled"] is False
    assert result["next_check"] is None
    assert result["stock_status"]["target_date"] is None
    assert result["stock_status"]["status"] == "calendar_unknown"
    assert result["post_close"]["sync"]["status"] == "unavailable"
    assert result["post_close"]["review"]["status"] == "not_run"
