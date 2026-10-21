"""Bounded authenticated read/SSE load against the deployed production edge.

Credentials come from the environment and never enter reports. Each virtual
client is a concurrent connection using one account, not an independent tenant.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import resource
import time
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

import httpx

from scripts.observe_candidate_soak import write_json

ROUTES = (
    "/api/overview/market", "/api/intraday/status", "/api/screener/limit-ladder",
    "/api/watchlist", "/api/strategies", "/api/capabilities", "/api/account/tenants",
)


def valid_payload(path: str, value: object) -> bool:
    if path == "/api/overview/market":
        return isinstance(value, dict) and {"as_of", "quote_status", "breadth"} <= value.keys()
    if path == "/api/intraday/status":
        return isinstance(value, dict) and {"enabled", "running", "last_fetch_ms"} <= value.keys()
    if path == "/api/screener/limit-ladder":
        return isinstance(value, dict) and {"as_of", "tiers", "counts"} <= value.keys()
    if path == "/api/account/tenants":
        return isinstance(value, (dict, list)) and bool(value)
    fields = {
        "/api/watchlist": "symbols", "/api/strategies": "strategies",
        "/api/capabilities": "capabilities",
    }
    return isinstance(value, dict) and fields.get(path) in value


def latency_summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        **{
            name: round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3) if ordered else None
            for name, fraction in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99), ("max_ms", 1))
        },
    }


def next_slot(previous: float, now: float, rps: int) -> tuple[float, int]:
    planned = previous + 1 / rps
    missed = max(0, math.floor((now - planned) * rps) + 1) if now > planned else 0
    return planned + missed / rps, missed


def stage_passed(report: dict) -> bool:
    return (
        report["completed_duration"] and report["http"]["errors"] == 0
        and report["http"]["latency"]["count"] >= report["duration_seconds"] * report["target_rps"] * .9
        and report["http"]["latency"]["p95_ms"] <= 2000
        and report["http"]["latency"]["p99_ms"] <= 5000
        and report["sse"]["errors"] == 0
        and report["sse"]["max_active"] == report["connections"]
        and report["sse"]["connection_seconds"] >= report["connections"] * report["duration_seconds"] * .9
        and report["sse"]["heartbeat_frames"] >= report["connections"]
        and report["guard_failures"] == 0
        and report.get("generator", {}).get("max_loop_lag_ms", 0) <= 500
        and report.get("generator", {}).get("skipped_request_slots", 0)
        <= report["duration_seconds"] * report["target_rps"] * .05
    )


async def run_stage(
    client: httpx.AsyncClient, stream_clients: list[httpx.AsyncClient],
    guard_client: httpx.AsyncClient, spec: tuple[int, int, int],
    run_dir: Path, monitor: Path,
) -> dict:
    connections, duration, rps = spec
    started = time.monotonic()
    deadline = started + duration
    stop = asyncio.Event()
    latencies: list[float] = []
    routes: dict[str, list[float]] = {path: [] for path in ROUTES}
    errors: Counter = Counter()
    error_details: list[dict] = []
    statuses: Counter = Counter()
    sse = {
        "active": 0, "max_active": 0, "opened": 0, "errors": 0,
        "connection_seconds": 0.0, "heartbeat_frames": 0, "event_frames": 0,
        "planned_renewals": 0,
    }
    inflight = 0
    max_inflight = 0
    guard_failures = 0
    overload_ticks = 0
    sequence = 0
    max_loop_lag = 0.0
    max_schedule_lag = 0.0
    skipped_slots = 0
    cpu_started = resource.getrusage(resource.RUSAGE_SELF)

    async def stream_worker(index: int) -> None:
        # Spread the initial authorization burst over five seconds.
        await asyncio.sleep(index * 5 / connections)
        stream_client = stream_clients[index % len(stream_clients)]
        while time.monotonic() < deadline and not stop.is_set():
            connected_at = None
            try:
                ticket = await stream_client.post("/api/events/ticket")
                if ticket.status_code != 200:
                    raise ValueError(f"ticket_status_{ticket.status_code}")
                stream_path = ticket.json().get("stream", "")
                if not stream_path.startswith("/api/events?ticket="):
                    raise ValueError("invalid_ticket_response")
                # Renew before the production 60-second authorization TTL.
                lifetime = min(50, deadline - time.monotonic())
                if lifetime <= 0:
                    break
                async with asyncio.timeout(lifetime):
                    async with stream_client.stream("GET", stream_path, timeout=httpx.Timeout(20, read=25)) as response:
                        if response.status_code != 200 or "text/event-stream" not in response.headers.get("content-type", ""):
                            raise ValueError("invalid_sse_response")
                        connected_at = time.monotonic()
                        sse["active"] += 1
                        sse["max_active"] = max(sse["max_active"], sse["active"])
                        sse["opened"] += 1
                        async for line in response.aiter_lines():
                            if stop.is_set():
                                break
                            if line.startswith(":"):
                                sse["heartbeat_frames"] += 1
                            elif line.startswith("event:"):
                                if "access_revoked" in line:
                                    raise ValueError("premature_access_revoked")
                                sse["event_frames"] += 1
                        if not stop.is_set() and time.monotonic() < deadline - 1:
                            raise ValueError("premature_sse_eof")
            except TimeoutError:
                sse["planned_renewals"] += 1
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                sse["errors"] += 1
                errors[f"sse:{type(exc).__name__}"] += 1
                stop.set()
            finally:
                if connected_at is not None:
                    sse["active"] -= 1
                    sse["connection_seconds"] += time.monotonic() - connected_at

    async def request_one(path: str) -> None:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        begin = time.monotonic()
        try:
            response = await client.get(path)
            statuses[str(response.status_code)] += 1
            if response.status_code != 200:
                errors[f"http:{response.status_code}"] += 1
                error_details.append({
                    "path": path, "status": response.status_code,
                    "elapsed_ms": round((time.monotonic() - begin) * 1000, 3),
                    "detail": str(response.json().get("detail", ""))[:200],
                })
                if response.status_code >= 500:
                    stop.set()
            elif not valid_payload(path, response.json()):
                errors[f"schema:{path}"] += 1
                stop.set()
        except (httpx.HTTPError, ValueError) as exc:
            errors[f"http:{type(exc).__name__}"] += 1
            stop.set()
        finally:
            elapsed = (time.monotonic() - begin) * 1000
            latencies.append(elapsed)
            routes[path].append(elapsed)
            inflight -= 1

    async def produce() -> None:
        nonlocal sequence, max_schedule_lag, skipped_slots
        tasks: set[asyncio.Task] = set()
        next_request = time.monotonic()
        while time.monotonic() < deadline and not stop.is_set():
            if len(tasks) >= connections:
                errors["generator_inflight_limit"] += 1
                stop.set()
                break
            task = asyncio.create_task(request_one(ROUTES[sequence % len(ROUTES)]))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            sequence += 1
            # Never replay missed slots in a burst when the generator stalls.
            current_time = time.monotonic()
            max_schedule_lag = max(max_schedule_lag, current_time - next_request - 1 / rps)
            next_request, missed = next_slot(next_request, current_time, rps)
            skipped_slots += missed
            await asyncio.sleep(max(0, next_request - time.monotonic()))
        if tasks:
            await asyncio.gather(*tasks)

    async def guard() -> None:
        nonlocal guard_failures, overload_ticks
        while time.monotonic() < deadline and not stop.is_set():
            failed = False
            try:
                response = await guard_client.get("/health/ready")
                failed = response.status_code != 200 or response.json().get("ready") is not True
                status = json.loads((monitor / "status.json").read_text())
                sample = status["last_sample"]
                sample_age = time.time() - datetime.fromisoformat(sample["at"]).timestamp()
                failed |= sample_age > 75 or bool(sample["failures"])
                for value in sample["containers"].values():
                    limit = value.get("memory_limit_bytes")
                    if limit:
                        failed |= value.get("memory_bytes", limit) / limit > .9
            except (httpx.HTTPError, OSError, ValueError, KeyError):
                failed = True
            overload_ticks = overload_ticks + 1 if inflight >= connections * .8 else 0
            if failed or overload_ticks >= 3:
                guard_failures += 1
                stop.set()
            write_json(run_dir / "progress.json", {
                "connections": connections, "elapsed_seconds": round(time.monotonic() - started, 1),
                "target_rps": rps, "requests": len(latencies), "statuses": dict(statuses),
                "errors": dict(errors), "sse": sse, "http_inflight": inflight,
                "latency": latency_summary(latencies), "guard_failures": guard_failures,
                "max_loop_lag_ms": round(max_loop_lag * 1000, 3),
                "max_schedule_lag_ms": round(max_schedule_lag * 1000, 3),
                "skipped_request_slots": skipped_slots,
            })
            await asyncio.sleep(5)

    async def loop_probe() -> None:
        nonlocal max_loop_lag
        while time.monotonic() < deadline and not stop.is_set():
            before = time.monotonic()
            await asyncio.sleep(.1)
            max_loop_lag = max(max_loop_lag, time.monotonic() - before - .1)

    streams = [asyncio.create_task(stream_worker(index)) for index in range(connections)]
    try:
        await asyncio.gather(produce(), guard(), loop_probe())
        stop.set()
    finally:
        for task in streams:
            task.cancel()
        await asyncio.gather(*streams, return_exceptions=True)
    elapsed = time.monotonic() - started
    cpu_finished = resource.getrusage(resource.RUSAGE_SELF)
    http_errors = sum(number for key, number in errors.items() if not key.startswith("sse:"))
    report = {
        "connections": connections, "duration_seconds": duration, "target_rps": rps,
        "elapsed_seconds": round(elapsed, 3),
        "completed_duration": elapsed >= duration - 1 and not errors and not guard_failures,
        "http": {
            "errors": http_errors, "statuses": dict(statuses), "latency": latency_summary(latencies),
            "achieved_rps": round(len(latencies) / elapsed, 2), "max_inflight": max_inflight,
            "routes": {path: latency_summary(values) for path, values in routes.items()},
        },
        "sse": {**sse, "connection_seconds": round(sse["connection_seconds"], 2)},
        "errors": dict(errors), "error_details": error_details, "guard_failures": guard_failures,
        "generator": {
            "max_loop_lag_ms": round(max_loop_lag * 1000, 3),
            "max_schedule_lag_ms": round(max_schedule_lag * 1000, 3),
            "skipped_request_slots": skipped_slots,
            "cpu_seconds": round(
                cpu_finished.ru_utime + cpu_finished.ru_stime
                - cpu_started.ru_utime - cpu_started.ru_stime, 3,
            ),
            "sse_client_pool_count": len(stream_clients),
            "rest_pool_separate": True,
        },
    }
    report["passed"] = stage_passed(report)
    write_json(run_dir / f"stage-{connections}.json", report)
    return report


async def run(args) -> None:
    args.run_dir.mkdir(parents=True, exist_ok=False)
    stages = [tuple(map(int, value.split(":"))) for value in args.stage]
    if any(len(value) != 3 or not 1 <= value[0] <= 500 or not 30 <= value[1] <= 900 or not 1 <= value[2] <= 150 for value in stages):
        raise ValueError("stage limits: connections 1..500, duration 30..900s, rate 1..150rps")
    async with AsyncExitStack() as resources:
        def make_client(limit: int, cookies=None):
            return httpx.AsyncClient(
                base_url=args.origin, headers={"Origin": args.origin}, verify=not args.insecure,
                trust_env=False, timeout=10, cookies=cookies,
                limits=httpx.Limits(max_connections=limit, max_keepalive_connections=limit),
            )

        client = await resources.enter_async_context(make_client(500))
        login = await client.post("/api/auth/login", json={
            "email": os.environ["TSP_LOAD_EMAIL"], "password": os.environ["TSP_LOAD_PASSWORD"],
        })
        if login.status_code != 200:
            raise RuntimeError(f"authentication unavailable: {login.status_code}; no retry")
        # HTTPX scans its pool for each request. Keep idle SSE connections out of
        # the REST pool and bound each SSE pool to avoid testing the driver itself.
        stream_clients = [
            await resources.enter_async_context(make_client(80, client.cookies))
            for _ in range(math.ceil(max(stage[0] for stage in stages) / 50))
        ]
        guard_client = await resources.enter_async_context(make_client(2, client.cookies))
        results = []
        try:
            for spec in stages:
                result = await run_stage(
                    client, stream_clients, guard_client, spec, args.run_dir, args.monitor_dir,
                )
                results.append(result)
                if not result["passed"]:
                    break
        finally:
            await client.post("/api/auth/logout")
        write_json(args.run_dir / "report.json", {
            "status": "succeeded" if len(results) == len(stages) and all(row["passed"] for row in results) else "failed",
            "scope": "one_account_read_only_http_and_concurrent_sse_connections",
            "origin": args.origin, "stages": results, "independent_tenant_capacity_certified": False,
            "complete_trading_day_certified": False,
        })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--monitor-dir", type=Path, required=True)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--stage", action="append", required=True, help="connections:seconds:rps")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
