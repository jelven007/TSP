"""Durable read-only production observation for publication/retention timeouts.

Run on the Linux Docker host. No synthetic quotes, archive settings, or business
writes are made. A future trading day remains pending until its actual window ends.
"""
from __future__ import annotations

import argparse
import fcntl
import http.cookiejar
import json
import os
import ssl
import subprocess
import time
import urllib.request
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from app.market_time import CN_TZ, published_trading_day, trading_calendar_provenance
from scripts.observe_candidate_soak import disk_snapshot, parse_endpoints, probe, write_json


def command(args: list[str]) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=15,
    ).stdout.strip()


def container_metrics(names: list[str]) -> dict:
    values = {}
    for item in json.loads(command(["docker", "inspect", *names])):
        state = item["State"]
        value = {
            "running": state["Running"],
            "healthy": state.get("Health", {}).get("Status") == "healthy",
            "started_at": state["StartedAt"], "image_id": item["Image"],
            "restart_count": item["RestartCount"], "oom_killed": state["OOMKilled"],
        }
        try:
            lines = Path(f"/proc/{state['Pid']}/cgroup").read_text().splitlines()
            relative = next(line[3:] for line in lines if line.startswith("0::"))
            cgroup = Path("/sys/fs/cgroup") / relative.lstrip("/")
            value["memory_bytes"] = int((cgroup / "memory.current").read_text())
            limit = (cgroup / "memory.max").read_text().strip()
            value["memory_limit_bytes"] = None if limit == "max" else int(limit)
            value["memory_events"] = {
                key: int(number)
                for key, number in (
                    line.split() for line in (cgroup / "memory.events").read_text().splitlines()
                )
            }
            value["cpu_usage_usec"] = int(dict(
                line.split() for line in (cgroup / "cpu.stat").read_text().splitlines()
            )["usage_usec"])
        except (OSError, ValueError, StopIteration, KeyError) as exc:
            value["metrics_error"] = type(exc).__name__
        values[item["Name"].lstrip("/")] = value
    return values


def platform_quotes(container: str) -> dict:
    # Read the platform token inside its existing process boundary; never emit it.
    code = (
        "import os,json,urllib.request; "
        "r=urllib.request.Request('http://127.0.0.1:3018/api/intraday/status',"
        "headers={'Authorization':'Bearer '+os.environ['TSP_PLATFORM_MARKET_TOKEN']}); "
        "v=json.load(urllib.request.urlopen(r,timeout=5)); "
        "keys=('enabled','running','paused','symbol_count','last_fetch_ms','quote_age_ms',"
        "'is_polling_window','market_phase','final_sync_done','final_sync_failed'); "
        "print(json.dumps({k:v.get(k) for k in keys}))"
    )
    return json.loads(command(["docker", "exec", container, "/app/.venv/bin/python", "-c", code]))


def maintenance_snapshot() -> dict:
    timer = dict(line.split("=", 1) for line in command([
        "systemctl", "show", "tsp-public-release-maintenance.timer",
        "-p", "ActiveState", "-p", "LastTriggerUSec",
    ]).splitlines())
    service = dict(line.split("=", 1) for line in command([
        "systemctl", "show", "tsp-public-release-maintenance.service",
        "-p", "Result", "-p", "ActiveState",
    ]).splitlines())
    last_run = datetime.fromisoformat(" ".join(timer["LastTriggerUSec"].split()[1:3])).replace(tzinfo=CN_TZ)
    return {
        "active": timer["ActiveState"] == "active",
        "last_run_age_seconds": time.time() - last_run.timestamp(),
        "result": service["Result"], "service_state": service["ActiveState"],
    }


class BusinessProbe:
    def __init__(self) -> None:
        self.origin = "https://127.0.0.1"
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
        )
        self.request("/api/auth/login", {
            "email": os.environ["TSP_LOAD_EMAIL"], "password": os.environ["TSP_LOAD_PASSWORD"],
        })

    def request(self, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Origin": self.origin, "Content-Type": "application/json"},
        )
        with self.opener.open(request, timeout=10) as response:
            value = json.load(response)
        if path == "/api/watchlist" and (not isinstance(value, dict) or "symbols" not in value):
            raise ValueError("invalid_watchlist_response")
        return value


def take_sample(config: dict, business: BusinessProbe) -> dict:
    now = datetime.now(CN_TZ)
    value = {"at": now.isoformat()}
    for key, collect in (
        ("containers", lambda: container_metrics(config["containers"])),
        ("disks", lambda: disk_snapshot([Path(config["public_root"])])),
        ("quotes", lambda: platform_quotes(config["platform_container"])),
        ("maintenance", maintenance_snapshot),
    ):
        try:
            value[key] = collect()
        except Exception as exc:
            value[key] = {"error": type(exc).__name__}
    try:
        begin = time.monotonic()
        business.request("/api/watchlist")
        value["business"] = {"ok": True, "latency_ms": (time.monotonic() - begin) * 1000}
    except Exception as exc:
        value["business"] = {"ok": False, "error": type(exc).__name__}
    value["endpoints"] = {name: probe(url) for name, url in config["endpoints"].items()}
    try:
        root = Path(config["public_root"])
        release = json.loads((root / "current" / ".public_release.json").read_text())
        value["publication"] = {
            "id": release["id"], "published_at": release["published_at"],
            "age_seconds": now.timestamp() - release["published_at"],
            "release_count": sum(path.is_dir() for path in (root / "releases").iterdir()),
        }
    except Exception as exc:
        value["publication"] = {"error": type(exc).__name__}
    # statvfs inode measurements are required: free bytes alone missed this incident.
    try:
        stat = os.statvfs(config["public_root"])
        value["capacity"] = {
            "free_inodes": stat.f_favail, "total_inodes": stat.f_files,
            "free_bytes": stat.f_bavail * stat.f_frsize,
        }
    except OSError as exc:
        value["capacity"] = {"error": type(exc).__name__}
    return value


def sample_failures(sample: dict, baseline: dict) -> list[str]:
    failures = []
    if sample.get("business", {}).get("ok") is not True:
        failures.append("business_read")
    if not sample.get("endpoints") or any(
        endpoint.get("ok") is not True for endpoint in sample["endpoints"].values()
    ):
        failures.append("endpoint_readiness")
    containers = sample.get("containers", {})
    if not baseline or set(containers) != set(baseline):
        failures.append("container_evidence_missing")
    for name, initial in baseline.items():
        value = containers.get(name, {})
        if (
            value.get("running") is not True or value.get("healthy") is not True
            or value.get("oom_killed") is not False
            or any(value.get(key) != initial.get(key) for key in (
                "restart_count", "started_at", "image_id",
            ))
            or value.get("memory_events", {}).get("oom_kill") is None
            or value.get("memory_events", {}).get("oom_kill") != initial.get("memory_events", {}).get("oom_kill")
        ):
            failures.append(f"container:{name}")
    capacity = sample.get("capacity", {})
    if (
        capacity.get("free_bytes", 0) < 1024**3
        or capacity.get("free_inodes", 0) < max(100_000, capacity.get("total_inodes", 0) // 10)
    ):
        failures.append("capacity_reserve")
    maintenance = sample.get("maintenance", {})
    if (
        maintenance.get("active") is not True or maintenance.get("result") != "success"
        or not 0 <= maintenance.get("last_run_age_seconds", -1) <= 660
    ):
        failures.append("retention_timer")
    publication = sample.get("publication", {})
    if not publication.get("id") or not 0 <= publication.get("age_seconds", -1) <= 660:
        failures.append("publication_freshness")
    return failures


def day_window(day: date) -> tuple[datetime, datetime]:
    return (
        datetime(day.year, day.month, day.day, 9, 15, tzinfo=CN_TZ),
        datetime(day.year, day.month, day.day, 16, 30, tzinfo=CN_TZ),
    )


def evaluate_day(samples: list[dict], config: dict, now: datetime, baseline: dict) -> dict:
    day = date.fromisoformat(config["trading_day"])
    start, end = day_window(day)
    interval = config["interval_seconds"]
    rows = [row for row in samples if start <= datetime.fromisoformat(row["at"]) <= end + timedelta(seconds=interval)]
    times = [datetime.fromisoformat(row["at"]) for row in rows]
    gaps = [(right - left).total_seconds() for left, right in pairwise(times)]
    completed = now >= end
    coverage = (
        completed and bool(times) and times[0] <= start + timedelta(seconds=interval)
        and times[-1] >= end - timedelta(seconds=interval)
        and max(gaps, default=0) <= interval * 2.5
    )
    failure_counts: dict[str, int] = {}
    for row in rows:
        for failure in sample_failures(row, baseline):
            failure_counts[failure] = failure_counts.get(failure, 0) + 1
    # Allow one minute for session opening. Lunch and after close are deliberately
    # not subject to continuous-quote freshness.
    sessions = ((9, 16, 9, 25), (9, 31, 11, 30), (13, 1, 15, 0))
    quote_checks = []
    for sh, sm, eh, em in sessions:
        left = start.replace(hour=sh, minute=sm)
        right = start.replace(hour=eh, minute=em)
        session_rows = [row for row in rows if left <= datetime.fromisoformat(row["at"]) < right]
        fetches = {row.get("quotes", {}).get("last_fetch_ms") for row in session_rows}
        fetches.discard(None)
        fresh = bool(session_rows) and len(fetches) >= 2 and all(
            row.get("quotes", {}).get("enabled") is True
            and row.get("quotes", {}).get("running") is True
            and row.get("quotes", {}).get("symbol_count", 0) > 0
            and row.get("quotes", {}).get("last_fetch_ms") is not None
            and 0 <= datetime.fromisoformat(row["at"]).timestamp() * 1000 - row["quotes"]["last_fetch_ms"] <= 60_000
            for row in session_rows
        )
        quote_checks.append(fresh)
    after = [
        row for row in rows if datetime.fromisoformat(row["at"]) >= start.replace(hour=15, minute=20)
    ]
    daily_ok = any(
        row.get("endpoints", {}).get("platform", {}).get("checks", {}).get("tasks", {})
        .get("last_pipeline", {}).get("status") == "succeeded"
        and str(row["endpoints"]["platform"]["checks"]["tasks"]["last_pipeline"].get("finished_at", ""))[:10] == day.isoformat()
        and datetime.fromisoformat(row["endpoints"]["platform"]["checks"]["tasks"]["last_pipeline"]["finished_at"])
        .astimezone(CN_TZ).hour >= 15
        and all(
            row["endpoints"]["platform"].get("checks", {}).get("data_freshness", {}).get(key) == day.isoformat()
            for key in ("stock_daily_latest", "stock_enriched_latest", "stock_minute_latest", "auction_snapshot_latest")
        )
        for row in after
    )
    releases = {row.get("publication", {}).get("id") for row in rows}
    releases.discard(None)
    # API checks run before current is read. Permit a single publication boundary
    # mismatch but fail a reader that still has the same mismatch next sample.
    stale_pairs = [
        row.get("endpoints", {}).get("api", {}).get("checks", {}).get("public_files", {}).get("release_id")
        != row.get("publication", {}).get("id")
        for row in rows
    ]
    checks = {
        "published_trading_day": published_trading_day(day) is True,
        "actual_full_window": coverage,
        "all_services_and_capacity": bool(rows) and not failure_counts,
        "auction_and_both_sessions_fresh": all(quote_checks),
        "postclose_pipeline_and_datasets": daily_ok,
        "publication_continues": len(releases) >= 60,
        "api_follows_current": bool(rows) and not any(a and b for a, b in pairwise(stale_pairs)),
    }
    load_runs = {}
    for name, report_path in config.get("trading_load_reports", {}).items():
        try:
            result = json.loads(Path(report_path).read_text())
            stages = result.get("stages", [])
            passed = result.get("status") == "succeeded" and any(
                stage.get("connections") == 500 and stage.get("duration_seconds", 0) >= 600
                and stage.get("target_rps", 0) >= 120 and stage.get("passed") is True
                for stage in stages
            )
            load_runs[name] = {"status": result.get("status"), "passed": passed, "path": report_path}
        except (OSError, ValueError):
            load_runs[name] = {"status": "pending", "passed": False, "path": report_path}
    if load_runs:
        checks["both_trading_session_loads"] = (
            set(load_runs) == {"morning", "afternoon"}
            and all(result["passed"] for result in load_runs.values())
        )
    return {
        "status": ("succeeded" if all(checks.values()) else "failed") if completed else "pending",
        "complete_trading_day": completed and all(checks.values()),
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "evaluated_at": now.isoformat(), "samples": len(rows),
        "checks": checks, "failure_counts": failure_counts,
        "max_gap_seconds": max(gaps, default=0), "distinct_publications": len(releases),
        "minimum_free_inodes": min((row.get("capacity", {}).get("free_inodes", 0) for row in rows), default=None),
        "calendar": trading_calendar_provenance(),
        "trading_load_runs": load_runs,
        "scope": "production_timeout_health_quotes_publication_retention",
        "limitations": ["Concurrent connections do not certify independent tenant capacity.", "Second quote archive settings remain unchanged."],
    }


def write_day_markdown(run_dir: Path, report: dict) -> None:
    # Chinese report punctuation is intentional.
    # ruff: noqa: RUF001
    lines = [
        "# 完整交易日与并发验证",
        "",
        f"状态：{report['status']}；完整交易日通过：{report['complete_trading_day']}。",
        f"窗口：{report['window_start']} 至 {report['window_end']}。",
        f"报告时间：{report['evaluated_at']}。",
        "",
        f"样本：{report['samples']}；最大间隔：{report['max_gap_seconds']} 秒；"
        f"不同快照：{report['distinct_publications']}；最低空闲 inode：{report['minimum_free_inodes']}。",
        "",
        "| 检查 | 通过 |",
        "|---|---|",
        *(f"| {name} | {passed} |" for name, passed in report["checks"].items()),
        "",
        "失败计数：" + json.dumps(report["failure_counts"], ensure_ascii=False),
        "",
        "盘中压测：" + json.dumps(report["trading_load_runs"], ensure_ascii=False),
        "",
        "同一账号下的连接压测不代表独立多租户容量；秒级行情归档配置保持现状。",
    ]
    from app.services.fs_utils import atomic_write_text

    atomic_write_text(run_dir / "report.md", "\n".join(lines) + "\n")


def observe(run_dir: Path) -> None:
    config = json.loads((run_dir / "config.json").read_text())
    end = day_window(date.fromisoformat(config["trading_day"]))[1]
    telemetry = run_dir / "telemetry.jsonl"
    baseline_path = run_dir / "baseline.json"
    with (run_dir / "observer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text())
        else:
            baseline = container_metrics(config["containers"])
            write_json(baseline_path, baseline)
        business = BusinessProbe()
        while True:
            started = time.monotonic()
            row = take_sample(config, business)
            row["failures"] = sample_failures(row, baseline)
            with telemetry.open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
            now = datetime.now(CN_TZ)
            samples = [json.loads(line) for line in telemetry.read_text().splitlines()]
            report = evaluate_day(samples, config, now, baseline)
            write_json(run_dir / "report.json", report)
            write_day_markdown(run_dir, report)
            write_json(run_dir / "status.json", {
                "status": "running" if now < end else report["status"],
                "heartbeat_at": now.isoformat(), "sample_count": len(samples),
                "last_sample": row, "trading_day_status": report["status"],
            })
            if now >= end:
                business.request("/api/auth/logout", {})
                break
            time.sleep(max(0, config["interval_seconds"] - (time.monotonic() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--trading-day", type=date.fromisoformat)
    parser.add_argument("--endpoint", action="append")
    parser.add_argument("--container", action="append")
    parser.add_argument("--platform-container", default="tsp-full-platform-1")
    parser.add_argument("--public-root", default="/opt/tsp-full/candidate/public")
    args = parser.parse_args()
    if args.arm:
        if not args.trading_day or published_trading_day(args.trading_day) is not True:
            parser.error("arming requires a confirmed published trading day")
        if datetime.now(CN_TZ) >= day_window(args.trading_day)[0]:
            parser.error("arm before the actual trading-day window begins")
        if not args.endpoint or not args.container:
            parser.error("explicit production endpoints and containers are required")
        args.run_dir.mkdir(parents=True, exist_ok=False)
        write_json(args.run_dir / "config.json", {
            "trading_day": args.trading_day.isoformat(), "interval_seconds": 30,
            "endpoints": parse_endpoints(args.endpoint), "containers": args.container,
            "platform_container": args.platform_container, "public_root": args.public_root,
            "armed_at": datetime.now(CN_TZ).isoformat(),
        })
    else:
        observe(args.run_dir)


if __name__ == "__main__":
    main()
