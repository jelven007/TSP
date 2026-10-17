"""Durable passive observation for a candidate API/platform/gateway stack.

This observer does not generate synthetic users or market traffic. It records
deep readiness, container identity/restarts, disk headroom, and sampling gaps.
Run from backend with ``-m scripts.observe_candidate_soak --help``.
"""
from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import suppress
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.market_time import CN_TZ
from app.services.fs_utils import atomic_write_text
from app.services.task_state import (
    TERMINAL_TASK_STATUSES,
    normalize_task_record,
    transition_task_record,
)

DEFAULT_ENDPOINTS = (
    "api=http://127.0.0.1:3018/health/ready",
    "platform=http://127.0.0.1:3028/health/ready",
    "gateway=http://127.0.0.1:3031/health/ready",
)
DEFAULT_CONTAINERS = ("tsp-local-api", "tsp-local-broker", "tsp-pg")
DOCKER_SOCKET = Path("/var/run/docker.sock")


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
    )


def parse_endpoints(values: list[str] | tuple[str, ...]) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, url = value.partition("=")
        if (
            not separator
            or not name
            or name in result
            or not url.startswith(("http://", "https://"))
        ):
            raise ValueError(f"invalid endpoint declaration: {value}")
        result[name] = url
    if not result:
        raise ValueError("at least one endpoint is required")
    return result


def compact_health(value: dict) -> dict:
    checks = {}
    for name, check in (value.get("checks") or {}).items():
        if not isinstance(check, dict):
            continue
        compact = {"status": check.get("status")}
        for key in (
            "release_id",
            "latest",
            "stock_daily_latest",
            "stock_enriched_latest",
            "stock_minute_latest",
            "auction_snapshot_latest",
            "last_marker_at",
            "reconnects",
            "connected",
            "listener_connected",
            "healthy",
            "active_id",
        ):
            if key in check:
                compact[key] = check[key]
        last_pipeline = check.get("last_pipeline")
        if isinstance(last_pipeline, dict):
            compact["last_pipeline"] = {
                key: last_pipeline.get(key)
                for key in ("id", "status", "finished_at")
            }
        checks[name] = compact
    return {
        "status": value.get("status"),
        "ready": value.get("ready"),
        "version": value.get("version"),
        "mode": value.get("mode"),
        "checks": checks,
    }


def probe(url: str) -> dict:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = json.load(response)
        result = compact_health(body)
        result.update(
            ok=response.status == 200 and body.get("ready") is True,
            status_code=response.status,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path):
        super().__init__("localhost", timeout=10)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


def _container_from_value(item: dict) -> dict:
    state = item.get("State") or {}
    return {
        "ok": state.get("Running") is True,
        "status": state.get("Status"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "exit_code": state.get("ExitCode"),
        "restart_count": item.get("RestartCount", 0),
        "image_id": item.get("Image"),
    }


def _container_snapshot_socket(names: list[str] | tuple[str, ...]) -> dict:
    values = {}
    for name in names:
        connection = UnixHTTPConnection(DOCKER_SOCKET)
        try:
            connection.request(
                "GET",
                f"/containers/{quote(name, safe='')}/json",
            )
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                values[name] = {
                    "ok": False,
                    "error": f"Docker API {response.status}: {body[-500:]!r}",
                }
                continue
            values[name] = _container_from_value(json.loads(body))
        except Exception as exc:
            values[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            connection.close()
    return values


def container_snapshot(names: list[str] | tuple[str, ...]) -> dict:
    if not names:
        return {}
    try:
        result = subprocess.run(
            ["docker", "inspect", *names],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return _container_snapshot_socket(names)
    if result.returncode:
        return {
            name: {
                "ok": False,
                "error": result.stderr.strip()[-1000:],
            }
            for name in names
        }
    values = {}
    for item in json.loads(result.stdout):
        values[item["Name"].lstrip("/")] = _container_from_value(item)
    return values


def disk_snapshot(paths: list[Path]) -> dict:
    values = {}
    for path in paths:
        try:
            stat = os.statvfs(path)
            values[str(path)] = {
                "ok": True,
                "free_bytes": stat.f_bavail * stat.f_frsize,
                "total_bytes": stat.f_blocks * stat.f_frsize,
            }
        except Exception as exc:
            values[str(path)] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return values


def arm(
    run_dir: Path,
    *,
    duration_hours: float,
    interval_seconds: int,
    endpoints: dict[str, str],
    containers: list[str],
    disk_paths: list[Path],
    now: datetime,
) -> dict:
    if not 0.01 <= duration_hours <= 168:
        raise ValueError("duration-hours must be in [0.01, 168]")
    if not 5 <= interval_seconds <= 300:
        raise ValueError("interval-seconds must be in [5, 300]")
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "status.json"
    if path.exists():
        raise FileExistsError(f"existing run must not be overwritten: {path}")
    end = now + timedelta(hours=duration_hours)
    record = normalize_task_record(
        {
            "id": run_dir.name,
            "status": "queued",
            "phase": "armed",
            "created_at": now.isoformat(),
            "window_start": now.isoformat(),
            "window_end": end.isoformat(),
            "duration_hours": duration_hours,
            "interval_seconds": interval_seconds,
            "endpoints": endpoints,
            "containers": containers,
            "disk_paths": [str(path.resolve()) for path in disk_paths],
            "heartbeat_at": now.isoformat(),
            "passive_observation": True,
            "load_generation": False,
        }
    )
    write_json(path, record)
    return record


def daemonize(run_dir: Path) -> dict:
    run_dir = run_dir.resolve()
    status_path = run_dir / "status.json"
    if not status_path.exists():
        raise FileNotFoundError("arm the observation before daemonizing")
    launch_path = run_dir / "launch.json"
    if launch_path.exists():
        raise FileExistsError(f"launch record already exists: {launch_path}")
    backend = Path(__file__).resolve().parents[1]
    stdout_path = run_dir / "observer.stdout.log"
    stderr_path = run_dir / "observer.stderr.log"
    with (
        stdout_path.open("ab", buffering=0) as stdout,
        stderr_path.open("ab", buffering=0) as stderr,
    ):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.observe_candidate_soak",
                "--run-dir",
                str(run_dir),
            ],
            cwd=backend,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "POLARS_MAX_THREADS": "2",
            },
        )
    launch = {
        "pid": process.pid,
        "started_at": datetime.now(CN_TZ).isoformat(),
        "command": [
            sys.executable,
            "-m",
            "scripts.observe_candidate_soak",
            "--run-dir",
            str(run_dir),
        ],
        "cwd": str(backend),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "detached_session": True,
    }
    write_json(launch_path, launch)
    return launch


class Observer:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "status.json"
        self.record = normalize_task_record(json.loads(self.path.read_text()))
        self.start = datetime.fromisoformat(self.record["window_start"])
        self.end = datetime.fromisoformat(self.record["window_end"])
        self.interval = int(self.record["interval_seconds"])
        self.endpoints = dict(self.record["endpoints"])
        self.containers = list(self.record["containers"])
        self.disk_paths = [Path(value) for value in self.record["disk_paths"]]

    def persist(self, now: datetime) -> None:
        self.record.update(
            heartbeat_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        write_json(self.path, self.record)

    def finish(
        self,
        status: str,
        now: datetime,
        error: str | None = None,
    ) -> None:
        self.record = transition_task_record(
            self.record,
            status,
            now=now.isoformat(),
            error=error,
        )
        self.record["phase"] = "finished"
        self.persist(now)

    def begin(self, now: datetime) -> None:
        self.record = transition_task_record(
            self.record,
            "running",
            now=now.isoformat(),
        )
        self.record.update(
            phase="observing",
            start_lateness_seconds=max(
                0,
                (now - self.start).total_seconds(),
            ),
            initial_containers=container_snapshot(self.containers),
        )
        self.persist(now)

    def sample(self, now: datetime) -> dict:
        containers = container_snapshot(self.containers)
        initial = self.record.get("initial_containers", {})
        if containers and all(
            value.get("ok") is True for value in containers.values()
        ) and any(
            initial.get(name, {}).get("ok") is not True
            for name in self.containers
        ):
            self.record["initial_containers"] = containers
            self.record["container_baseline_at"] = now.isoformat()
        value = {
            "at": now.isoformat(),
            "endpoints": {
                name: probe(url)
                for name, url in self.endpoints.items()
            },
            "containers": containers,
            "disks": disk_snapshot(self.disk_paths),
        }
        with (self.run_dir / "telemetry.jsonl").open(
            "a",
            encoding="utf-8",
        ) as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.record["sample_count"] = self.record.get("sample_count", 0) + 1
        self.record["last_sample"] = value
        self.persist(now)
        return value

    def evaluate(self, now: datetime) -> dict:
        telemetry = [
            json.loads(line)
            for line in (self.run_dir / "telemetry.jsonl").read_text().splitlines()
        ]
        times = [datetime.fromisoformat(row["at"]) for row in telemetry]
        gaps = [
            (right - left).total_seconds()
            for left, right in pairwise(times)
        ]
        endpoint_summary = {}
        for name in self.endpoints:
            samples = [row["endpoints"].get(name, {}) for row in telemetry]
            latencies = [
                float(sample["latency_ms"])
                for sample in samples
                if sample.get("latency_ms") is not None
            ]
            versions = sorted({
                str(sample["version"])
                for sample in samples
                if sample.get("version") is not None
            })
            endpoint_summary[name] = {
                "samples": len(samples),
                "ready_samples": sum(sample.get("ok") is True for sample in samples),
                "failures": sum(sample.get("ok") is not True for sample in samples),
                "max_latency_ms": max(latencies, default=None),
                "versions": versions,
            }
        final_containers = container_snapshot(self.containers)
        initial_containers = self.record.get("initial_containers", {})
        container_summary = {}
        for name in self.containers:
            initial = initial_containers.get(name, {})
            final = final_containers.get(name, {})
            container_summary[name] = {
                "running": final.get("ok") is True,
                "restart_delta": (
                    int(final.get("restart_count", 0))
                    - int(initial.get("restart_count", 0))
                ),
                "image_stable": (
                    initial.get("image_id") is not None
                    and initial.get("image_id") == final.get("image_id")
                ),
                "started_at_stable": (
                    initial.get("started_at") is not None
                    and initial.get("started_at") == final.get("started_at")
                ),
            }
        disk_values = [
            disk
            for row in telemetry
            for disk in row["disks"].values()
            if disk.get("ok")
        ]
        observed_seconds = (
            (times[-1] - times[0]).total_seconds()
            if len(times) > 1
            else 0
        )
        checks = {
            "full_duration_observed": (
                bool(times)
                and times[0] <= self.start + timedelta(seconds=self.interval * 2)
                and times[-1] >= self.end - timedelta(seconds=self.interval * 2)
            ),
            "sampling_continuous": (
                bool(times)
                and max(gaps, default=0) <= self.interval * 2.5
            ),
            "all_endpoints_ready": all(
                summary["failures"] == 0
                for summary in endpoint_summary.values()
            ),
            "container_identity_stable": all(
                value["running"]
                and value["restart_delta"] == 0
                and value["image_stable"]
                and value["started_at_stable"]
                for value in container_summary.values()
            ),
            "disk_headroom": (
                bool(disk_values)
                and min(value["free_bytes"] for value in disk_values) >= 1024**3
            ),
        }
        failures = [name for name, passed in checks.items() if not passed]
        duration_hours = float(self.record["duration_hours"])
        report = {
            "status": "succeeded" if not failures else "failed",
            "scope": "passive_candidate_availability_observation",
            "window_start": self.start.isoformat(),
            "window_end": self.end.isoformat(),
            "evaluated_at": now.isoformat(),
            "configured_duration_hours": duration_hours,
            "observed_seconds": observed_seconds,
            "sample_count": len(telemetry),
            "max_sample_gap_seconds": max(gaps, default=0),
            "checks": checks,
            "failures": failures,
            "endpoints": endpoint_summary,
            "containers": container_summary,
            "minimum_free_bytes": min(
                (value["free_bytes"] for value in disk_values),
                default=None,
            ),
            "certification": {
                "passive_72h_observation": (
                    duration_hours >= 72
                    and observed_seconds >= 72 * 3600 - self.interval * 2
                    and not failures
                ),
                "active_100_user_load": False,
                "complete_trading_day": False,
            },
            "limitations": [
                "No synthetic users, requests, notifications, or market connections are created.",
                "Existing background work may continue, but this is not a controlled 100-user load.",
                "Host sleep, logout, shutdown, observer death, or health failure creates a measurable gap/failure.",
                "A complete trading day is evaluated by a separate armed observer.",
            ],
        }
        write_json(self.run_dir / "report.json", report)
        self.record["report_path"] = str(self.run_dir / "report.json")
        self.finish(
            report["status"],
            now,
            ", ".join(failures) if failures else None,
        )
        return report

    def step(self, now: datetime) -> None:
        if self.record["status"] in TERMINAL_TASK_STATUSES:
            return
        if self.record["status"] == "queued":
            self.begin(now)
        if self.record["status"] != "running":
            return
        if now >= self.end:
            self.sample(now)
            self.evaluate(now)
        else:
            self.sample(now)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--duration-hours", type=float, default=72)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--endpoint", action="append")
    parser.add_argument("--container", action="append")
    parser.add_argument("--disk-path", action="append", type=Path)
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--daemonize", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.run_dir = args.run_dir.resolve()
    now = datetime.now(CN_TZ)
    if args.arm:
        record = arm(
            args.run_dir,
            duration_hours=args.duration_hours,
            interval_seconds=args.interval_seconds,
            endpoints=parse_endpoints(args.endpoint or DEFAULT_ENDPOINTS),
            containers=args.container or list(DEFAULT_CONTAINERS),
            disk_paths=args.disk_path or [Path("../.tools/runtime")],
            now=now,
        )
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return
    if args.daemonize:
        print(json.dumps(daemonize(args.run_dir), ensure_ascii=False, indent=2))
        return

    with suppress(OSError):
        os.nice(10)
    with (args.run_dir / "observer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another observer owns this run")
        observer = Observer(args.run_dir)
        while observer.record["status"] not in TERMINAL_TASK_STATUSES:
            try:
                observer.step(datetime.now(CN_TZ))
            except Exception as exc:
                observer.finish(
                    "failed",
                    datetime.now(CN_TZ),
                    f"{type(exc).__name__}: {exc}",
                )
                raise
            if args.once or observer.record["status"] in TERMINAL_TASK_STATUSES:
                break
            time.sleep(observer.interval)
        print(json.dumps(observer.record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
