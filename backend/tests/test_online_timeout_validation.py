from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta

import httpx
import pytest

from scripts.performance.stress_online_timeout import (
    latency_summary,
    next_slot,
    run_stage,
    stage_passed,
    valid_payload,
)
from scripts.acceptance.validate_online_timeout import day_window, evaluate_day, sample_failures


def good_container():
    return {
        "running": True, "healthy": True, "restart_count": 0, "oom_killed": False,
        "started_at": "stable", "image_id": "fixed", "memory_events": {"oom_kill": 0},
    }


def day_evidence():
    start, end = day_window(date(2026, 10, 22))
    rows = []
    now = start
    while now <= end:
        release = str(int((now - start).total_seconds() // 300))
        rows.append({
            "at": now.isoformat(), "business": {"ok": True},
            "endpoints": {
                "api": {"ok": True, "checks": {"public_files": {"release_id": release}}},
                "platform": {"ok": True, "checks": {
                    "tasks": {"last_pipeline": {
                        "status": "succeeded", "finished_at": "2026-10-22T07:40:00+00:00",
                    }},
                    "data_freshness": dict.fromkeys(
                        ("stock_daily_latest", "stock_enriched_latest", "stock_minute_latest", "auction_snapshot_latest"),
                        "2026-10-22",
                    ),
                }},
            },
            "containers": {"api": good_container()},
            "capacity": {"free_bytes": 100 * 1024**3, "free_inodes": 6_000_000, "total_inodes": 13_000_000},
            "publication": {"id": release, "age_seconds": 60},
            "maintenance": {"active": True, "result": "success", "last_run_age_seconds": 60},
            "quotes": {"enabled": True, "running": True, "symbol_count": 5000, "last_fetch_ms": now.timestamp() * 1000 - 1000},
        })
        now += timedelta(seconds=30)
    return rows, {"trading_day": "2026-10-22", "interval_seconds": 30}, end, {"api": good_container()}


def test_complete_day_requires_actual_end_even_with_future_samples():
    rows, config, end, baseline = day_evidence()
    report = evaluate_day(rows, config, end - timedelta(hours=1), baseline)
    assert report["status"] == "pending"
    assert report["complete_trading_day"] is False
    completed = evaluate_day(rows, config, end, baseline)
    assert completed["status"] == "succeeded"
    assert completed["complete_trading_day"] is True


@pytest.mark.parametrize("problem", [
    "late", "gap", "missing_quotes", "stale_quotes", "low_inodes",
    "restart", "oom", "old_pipeline", "intraday_pipeline", "missing_minute", "stale_reader", "failed_retention",
])
def test_bad_day_evidence_cannot_certify(problem):
    rows, config, end, baseline = day_evidence()
    if problem == "late":
        rows = rows[3:]
    elif problem == "gap":
        del rows[10:15]
    elif problem == "missing_quotes":
        for row in rows:
            row["quotes"] = {}
    elif problem == "stale_quotes":
        for row in rows:
            row["quotes"]["last_fetch_ms"] = rows[0]["quotes"]["last_fetch_ms"]
    elif problem == "low_inodes":
        rows[25]["capacity"]["free_inodes"] = 5000
    elif problem == "restart":
        rows[25]["containers"]["api"]["started_at"] = "changed"
    elif problem == "oom":
        rows[25]["containers"]["api"]["memory_events"]["oom_kill"] = 1
    elif problem in {"old_pipeline", "intraday_pipeline"}:
        for row in rows:
            row["endpoints"]["platform"]["checks"]["tasks"]["last_pipeline"]["finished_at"] = (
                "2026-10-21T07:40:00+00:00" if problem == "old_pipeline" else "2026-10-22T05:40:00+00:00"
            )
    elif problem == "missing_minute":
        for row in rows:
            row["endpoints"]["platform"]["checks"]["data_freshness"]["stock_minute_latest"] = None
    elif problem == "stale_reader":
        for row in rows:
            row["endpoints"]["api"]["checks"]["public_files"]["release_id"] = "old"
    elif problem == "failed_retention":
        rows[25]["maintenance"]["result"] = "exit-code"
    report = evaluate_day(rows, config, end, baseline)
    assert report["status"] == "failed"
    assert report["complete_trading_day"] is False


def test_missing_capacity_and_container_metrics_are_failures():
    rows, _, _, baseline = day_evidence()
    row = deepcopy(rows[0])
    del row["capacity"]
    del row["containers"]["api"]["memory_events"]
    assert set(sample_failures(row, baseline)) == {"capacity_reserve", "container:api"}


def load_result():
    return {
        "completed_duration": True, "duration_seconds": 180, "target_rps": 40,
        "connections": 100, "guard_failures": 0,
        "http": {"errors": 0, "latency": {"count": 7200, "p95_ms": 500, "p99_ms": 1000}},
        "sse": {"errors": 0, "max_active": 100, "connection_seconds": 17500, "heartbeat_frames": 1000},
    }


@pytest.mark.parametrize("problem", [
    "duration", "errors", "slow", "queue", "sse_short", "no_ping", "guard", "driver_lag", "driver_skips",
])
def test_load_cannot_pass_when_duration_latency_or_stream_evidence_fails(problem):
    report = load_result()
    assert stage_passed(report)
    if problem == "duration":
        report["completed_duration"] = False
    elif problem == "errors":
        report["http"]["errors"] = 1
    elif problem == "slow":
        report["http"]["latency"]["p99_ms"] = 6000
    elif problem == "queue":
        report["http"]["latency"]["count"] = 5000
    elif problem == "sse_short":
        report["sse"]["connection_seconds"] = 100
    elif problem == "no_ping":
        report["sse"]["heartbeat_frames"] = 0
    elif problem == "guard":
        report["guard_failures"] = 1
    elif problem == "driver_lag":
        report["generator"] = {"max_loop_lag_ms": 1000}
    elif problem == "driver_skips":
        report["generator"] = {"skipped_request_slots": 1000}
    assert not stage_passed(report)


def test_payload_contract_and_nearest_rank_latency():
    assert latency_summary([1, 2, 100])["p99_ms"] == 100
    assert not valid_payload("/api/overview/market", {"detail": "unavailable"})
    assert valid_payload("/api/overview/market", {"as_of": "today", "quote_status": {}, "breadth": {}})
    assert not valid_payload("/api/screener/limit-ladder", {})
    assert valid_payload("/api/screener/limit-ladder", {"as_of": "today", "tiers": [], "counts": {}})


def test_pacing_skips_missed_slots_without_catchup_burst():
    assert next_slot(10, 10, 100) == (10.01, 0)
    future, missed = next_slot(10, 10.505, 100)
    assert missed == 50
    assert 10.505 < future <= 10.515


@pytest.mark.asyncio
async def test_load_stops_and_closes_streams_on_business_failure(tmp_path, monkeypatch):
    import json

    from app.market_time import CN_TZ

    closed = []

    class LiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": ping\n\n"
                await asyncio.sleep(.01)

        async def aclose(self):
            closed.append(True)

    def business(request):
        return httpx.Response(503, json={"detail": "market_history_unavailable"})

    def streaming(request):
        if request.method == "POST":
            return httpx.Response(200, json={"stream": "/api/events?ticket=test"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=LiveStream())

    monkeypatch.setattr(
        "scripts.performance.stress_online_timeout.ROUTES", ("/api/intraday/status",),
    )
    run_dir = tmp_path / "load"
    run_dir.mkdir()
    monitor = tmp_path / "monitor"
    monitor.mkdir()
    (monitor / "status.json").write_text(json.dumps({
        "last_sample": {"at": datetime.now(CN_TZ).isoformat(),
                        "failures": [], "containers": {}},
    }))
    async with (
        httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(business)) as client,
        httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(streaming)) as streams,
        httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ready": True}),
        )) as guard,
    ):
        result = await run_stage(client, [streams], guard, (2, 30, 10), run_dir, monitor)
    assert result["passed"] is False
    assert result["completed_duration"] is False
    assert result["elapsed_seconds"] < 10
    assert result["http"]["errors"] == 1
    assert result["error_details"][0]["path"] == "/api/intraday/status"
    assert result["sse"]["active"] == 0
    assert closed


def test_trading_day_cannot_pass_when_scheduled_load_is_missing_or_failed(tmp_path):
    import json

    rows, config, end, baseline = day_evidence()
    config["trading_load_reports"] = {
        name: str(tmp_path / f"{name}.json") for name in ("morning", "afternoon")
    }
    assert evaluate_day(rows, config, end, baseline)["status"] == "failed"
    result = {
        "status": "succeeded", "stages": [{
            "connections": 500, "duration_seconds": 600, "target_rps": 120, "passed": True,
        }],
    }
    for report_path in config["trading_load_reports"].values():
        from pathlib import Path

        Path(report_path).write_text(json.dumps(result))
    assert evaluate_day(rows, config, end, baseline)["status"] == "succeeded"
    result["status"] = "failed"
    (tmp_path / "afternoon.json").write_text(json.dumps(result))
    assert evaluate_day(rows, config, end, baseline)["status"] == "failed"
