"""Observe existing quote/minute writers; never open a market-data connection.

Run from backend with -m scripts.observe_second_acceptance --help.
State, telemetry and reports live under data/research/second_quote_acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from app.backtest.second_replay import SecondQuoteReplay
from app.market_scope import supported_symbol
from app.market_time import CN_TZ
from app.services.fs_utils import atomic_write_text
from app.services.task_state import (
    TERMINAL_TASK_STATUSES,
    normalize_task_record,
    transition_task_record,
)

PREFERENCE_KEYS = (
    "second_quote_archive_enabled", "second_quote_archive_retention_days",
    "second_quote_archive_window",
    "realtime_quotes_enabled", "realtime_pull_stock", "realtime_quote_interval",
    "minute_refresh_enabled", "minute_refresh_interval",
    "realtime_data_provider", "full_minute_data_provider",
)
PREFERENCE_DEFAULTS = {
    "second_quote_archive_enabled": False,
    "second_quote_archive_retention_days": 0,
    "second_quote_archive_window": "full",
    "realtime_quotes_enabled": True,
    "realtime_pull_stock": True,
    "realtime_quote_interval": 1.0,
    "minute_refresh_enabled": True,
    "minute_refresh_interval": 1,
    "realtime_data_provider": "rustdx",
    "full_minute_data_provider": "rustdx",
}
SAMPLE_SECONDS = 5
SETTLE_SECONDS = 20
FULL_DAY_MILESTONES = {
    "auction": (9, 25),
    "open": (9, 30),
    "morning_close": (11, 30),
    "afternoon_open": (13, 0),
    "close": (15, 0),
    "post_close": (15, 35),
}


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * ratio) - 1)], 3)


def next_candidate(now: datetime) -> date:
    """Weekday candidate only; the existing writers still apply trading-day gates."""
    day = now.date()
    if (now.hour, now.minute) >= (9, 30):
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def selected_preferences(
    data_dir: Path,
    preferences_path: Path | None = None,
) -> dict:
    path = preferences_path or data_dir / "user_data" / "preferences.json"
    value = json.loads(path.read_text())
    return {
        key: value.get(key, PREFERENCE_DEFAULTS[key])
        for key in PREFERENCE_KEYS
    }


def valid_preferences(value: dict) -> bool:
    return (
        all(value.get(key) is True for key in (
            "second_quote_archive_enabled", "realtime_quotes_enabled",
            "realtime_pull_stock", "minute_refresh_enabled",
        ))
        and value.get("second_quote_archive_window") == "full"
        and value.get("realtime_quote_interval") == 1
        and value.get("minute_refresh_interval") == 1
        and value.get("realtime_data_provider") == "rustdx"
        and value.get("full_minute_data_provider") == "rustdx"
    )


def health(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            result = json.load(response)
        return {
            "ok": result.get("ready") is True,
            "status": result.get("status"),
            "version": result.get("version"),
            "checks": result.get("checks"),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def minute_footer(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    # Hold one inode across atomic replacement by the live minute writer.
    with path.open("rb") as stream:
        metadata = pq.read_metadata(stream)
        column = metadata.schema.names.index("datetime")
        maxima = [
            stats.max for i in range(metadata.num_row_groups)
            if (stats := metadata.row_group(i).column(column).statistics) is not None
            and stats.has_min_max and stats.max is not None
        ]
        import os

        stat = os.fstat(stream.fileno())
    return {
        "exists": True, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        "rows": metadata.num_rows,
        "latest": max(maxima).isoformat() if maxima else None,
    }


def minute_coverage(path: Path, symbols: list[str], start: datetime, end: datetime) -> dict:
    if not path.exists():
        return {"coverage": 0.0, "symbols": 0, "fresh_symbols": 0, "rows": 0}
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        frame = pl.read_parquet(stream, columns=["symbol", "datetime"])
    # Minute files use Beijing wall-clock datetimes. Reject future rows explicitly.
    lo, hi = start.replace(tzinfo=None), end.replace(tzinfo=None)
    scoped = frame.filter(pl.col("symbol").is_in(symbols))
    future_rows = scoped.filter(pl.col("datetime") > hi + timedelta(minutes=1)).height
    frame = scoped.filter(pl.col("datetime").is_between(lo, hi))
    latest = frame.group_by("symbol").agg(pl.col("datetime").max())
    fresh = latest.filter(pl.col("datetime") >= hi - timedelta(seconds=120))
    missing = sorted(set(symbols) - set(fresh["symbol"].to_list()))
    return {
        "coverage": fresh.height / len(symbols), "symbols": latest.height,
        "fresh_symbols": fresh.height, "rows": frame.height,
        "oldest_symbol_latest": str(latest["datetime"].min()) if latest.height else None,
        "latest": str(latest["datetime"].max()) if latest.height else None,
        "missing_or_stale_symbols": missing, "partition_sha256": digest,
        "duplicate_keys": frame.select(pl.struct("symbol", "datetime").is_duplicated().sum()).item(),
        "future_rows": future_rows,
    }


def arm(
    run_dir: Path,
    data_dir: Path,
    day: date,
    minutes: int,
    now: datetime,
    *,
    profile: str = "open-window",
    preferences_path: Path | None = None,
) -> dict:
    if day.weekday() >= 5:
        raise ValueError("require a weekday candidate")
    if profile == "open-window":
        if not 1 <= minutes <= 60:
            raise ValueError("open-window profile requires a 1..60 minute window")
        start = datetime.combine(day, datetime.min.time(), CN_TZ).replace(
            hour=9,
            minute=30,
        )
        end = start + timedelta(minutes=minutes)
        segments = [{"name": "open", "start": start.isoformat(), "end": end.isoformat()}]
        minute_start, minute_end = start, end
        milestones = {}
    elif profile == "full-trading-day":
        start = datetime.combine(day, datetime.min.time(), CN_TZ).replace(
            hour=9,
            minute=15,
        )
        end = start.replace(hour=15, minute=40)
        morning_start = start.replace(hour=9, minute=30)
        morning_end = start.replace(hour=11, minute=30)
        afternoon_start = start.replace(hour=13, minute=0)
        afternoon_end = start.replace(hour=15, minute=0)
        segments = [
            {
                "name": "morning",
                "start": morning_start.isoformat(),
                "end": morning_end.isoformat(),
            },
            {
                "name": "afternoon",
                "start": afternoon_start.isoformat(),
                "end": afternoon_end.isoformat(),
            },
        ]
        minute_start, minute_end = morning_start, afternoon_end
        milestones = {
            name: start.replace(hour=hour, minute=minute).isoformat()
            for name, (hour, minute) in FULL_DAY_MILESTONES.items()
        }
    else:
        raise ValueError(f"unsupported observer profile: {profile}")
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "status.json"
    if path.exists():
        raise FileExistsError(f"existing run must not be overwritten: {path}")
    record = normalize_task_record({
        "id": run_dir.name, "status": "queued", "created_at": now.isoformat(),
        "phase": "waiting_for_market", "data_dir": str(data_dir.resolve()),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(), "profile": profile,
        "preferences_path": str(
            (preferences_path or data_dir / "user_data" / "preferences.json").resolve()
        ),
        "quality_segments": segments,
        "minute_window_start": minute_start.isoformat(),
        "minute_window_end": minute_end.isoformat(),
        "milestones": milestones,
        "candidate_trading_date": day.isoformat(),
        "trading_date_confirmed": False,
        "sample_seconds": SAMPLE_SECONDS, "settle_seconds": SETTLE_SECONDS,
        "thresholds": {
            "expected_interval_s": 1, "max_gap_s": 1.5,
            "stock_coverage": 1.0, "minute_coverage": 0.99,
            "minute_max_lag_s": 120, "max_observer_gap_s": 15,
        },
        "armed_preferences": selected_preferences(data_dir, preferences_path),
        "heartbeat_at": now.isoformat(),
    })
    write_json(path, record)
    return record


def daemonize(run_dir: Path, health_url: str) -> dict:
    run_dir = run_dir.resolve()
    if not (run_dir / "status.json").exists():
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
                "scripts.observe_second_acceptance",
                "--run-dir",
                str(run_dir),
                "--health-url",
                health_url,
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
            "scripts.observe_second_acceptance",
            "--run-dir",
            str(run_dir),
            "--health-url",
            health_url,
        ],
        "cwd": str(backend),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "detached_session": True,
    }
    write_json(launch_path, launch)
    return launch


class Observer:
    def __init__(self, run_dir: Path, health_url: str) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "status.json"
        self.record = normalize_task_record(json.loads(self.path.read_text()))
        self.data_dir = Path(self.record["data_dir"])
        self.start = datetime.fromisoformat(self.record["window_start"])
        self.end = datetime.fromisoformat(self.record["window_end"])
        self.segments = [
            (
                item["name"],
                datetime.fromisoformat(item["start"]),
                datetime.fromisoformat(item["end"]),
            )
            for item in self.record.get("quality_segments", [{
                "name": "window",
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
            }])
        ]
        self.minute_start = datetime.fromisoformat(
            self.record.get("minute_window_start", self.start.isoformat())
        )
        self.minute_end = datetime.fromisoformat(
            self.record.get("minute_window_end", self.end.isoformat())
        )
        self.preferences_path = Path(
            self.record.get(
                "preferences_path",
                self.data_dir / "user_data" / "preferences.json",
            )
        )
        self.day = self.start.date()
        self.minute_path = self.data_dir / "kline_minute" / f"date={self.day}" / "part.parquet"
        self.health_url = health_url
        self._health_at: datetime | None = None
        self._sessions: dict[str, tuple[tuple[int, int], dict]] = {}

    def persist(self, now: datetime) -> None:
        self.record.update(heartbeat_at=now.isoformat(), updated_at=now.isoformat())
        write_json(self.path, self.record)

    def finish(self, status: str, now: datetime, error: str | None = None) -> None:
        self.record = transition_task_record(self.record, status, now=now.isoformat(), error=error)
        self.record["phase"] = "finished"
        self.persist(now)

    def recover(self, now: datetime) -> None:
        if self.record["status"] == "running":
            self.finish("interrupted", now, "observer restarted during window; telemetry incomplete")

    def sessions(self) -> dict:
        result = {}
        for path in sorted((self.data_dir / "quote_second").glob("session-*.json")):
            stat = path.stat()
            sig = (stat.st_mtime_ns, stat.st_size)
            cached = self._sessions.get(path.name)
            if cached is None or cached[0] != sig:
                cached = (sig, json.loads(path.read_text()))
                self._sessions[path.name] = cached
            result[path.name] = cached[1]
        return result

    def begin(self, now: datetime) -> None:
        path = self.data_dir / "instruments" / "instruments.parquet"
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            stream.seek(0)
            frame = pl.read_parquet(stream, columns=["symbol"])
        symbols = sorted({
            str(symbol).strip().upper() for symbol in frame["symbol"]
            if supported_symbol(symbol, "stock")
        })
        if not symbols:
            self.finish("skipped_prerequisite", now, "empty supported A-share universe")
            return
        write_json(self.run_dir / "universe.json", {
            "symbols": symbols, "fingerprint": fingerprint(symbols),
            "instruments_sha256": digest, "frozen_at": now.isoformat(),
        })
        preferences = selected_preferences(
            self.data_dir,
            self.preferences_path,
        )
        if not valid_preferences(preferences):
            self.finish("skipped_prerequisite", now, "required archive/quote/minute preferences unavailable")
            return
        self.record = transition_task_record(self.record, "running", now=now.isoformat())
        self.record.update(
            phase="observing", expected_symbols=len(symbols),
            universe_fingerprint=fingerprint(symbols),
            start_lateness_s=max(0, (now - self.start).total_seconds()),
            start_preferences=preferences, config_fingerprint=fingerprint(preferences),
            initial_sessions=self.sessions(),
            initial_minute=minute_footer(self.minute_path),
        )
        self.persist(now)

    def sample(self, now: datetime) -> None:
        result: dict[str, Any] = {"at": now.isoformat()}
        errors = []
        for key, callback in (
            ("sessions", self.sessions),
            ("minute", lambda: minute_footer(self.minute_path)),
            (
                "preferences",
                lambda: selected_preferences(
                    self.data_dir,
                    self.preferences_path,
                ),
            ),
            ("free_bytes", lambda: shutil.disk_usage(self.data_dir).free),
        ):
            try:
                result[key] = callback()
            except Exception as exc:
                errors.append(f"{key}: {type(exc).__name__}: {exc}")
        if self._health_at is None or (now - self._health_at).total_seconds() >= 30:
            result["health"] = health(self.health_url)
            self._health_at = now
        result["errors"] = errors
        with (self.run_dir / "telemetry.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        self.record["sample_count"] = self.record.get("sample_count", 0) + 1
        self.record["last_sample"] = result
        self.persist(now)

    def evaluate(self, now: datetime) -> dict:
        self.record["phase"] = "validating"
        self.persist(now)
        universe = json.loads((self.run_dir / "universe.json").read_text())
        symbols = universe["symbols"]
        if fingerprint(symbols) != self.record["universe_fingerprint"]:
            raise ValueError("frozen universe changed")
        telemetry = [
            json.loads(line) for line in (self.run_dir / "telemetry.jsonl").read_text().splitlines()
        ]
        samples = [row for row in telemetry if self.start <= datetime.fromisoformat(row["at"]) <= self.end]
        replay = SecondQuoteReplay(self.data_dir)
        start_ms, end_ms = int(self.start.timestamp() * 1000), int(self.end.timestamp() * 1000)
        segment_quality = []
        for name, segment_start, segment_end in self.segments:
            result = replay.validate(
                self.day,
                self.day,
                symbols=symbols,
                start_ms=int(segment_start.timestamp() * 1000),
                end_ms=int(segment_end.timestamp() * 1000),
                trading_dates=[self.day],
                expected_interval_s=1,
                min_coverage_ratio=1,
            )
            segment_quality.append({
                "name": name,
                "start": segment_start.isoformat(),
                "end": segment_end.isoformat(),
                **result,
            })
        if len(segment_quality) == 1:
            quality = {
                key: value
                for key, value in segment_quality[0].items()
                if key not in {"name", "start", "end"}
            }
        else:
            quality = {
                "status": (
                    "ready"
                    if all(item["status"] == "ready" for item in segment_quality)
                    else "failed"
                ),
                "providers": sorted({
                    provider
                    for item in segment_quality
                    for provider in item.get("providers", [])
                }),
                "captures": sum(item.get("captures", 0) for item in segment_quality),
                "gap_count": sum(item.get("gap_count", 0) for item in segment_quality),
                "missing_dates": sorted({
                    value
                    for item in segment_quality
                    for value in item.get("missing_dates", [])
                }),
                "issues": [
                    f"{item['name']}: {issue}"
                    for item in segment_quality
                    for issue in item.get("issues", [])
                ],
                "segments": segment_quality,
            }
        manifest = replay.manifest(self.day, self.day)
        write_json(self.run_dir / "manifest.json", manifest)
        stamps, coverage = [], []
        gaps = []
        for _, segment_start, segment_end in self.segments:
            segment_stamps = []
            for snapshot in replay.iter_snapshots(
                self.day,
                self.day,
                symbols=symbols,
                asset_type="stock",
                start_ms=int(segment_start.timestamp() * 1000),
                end_ms=int(segment_end.timestamp() * 1000),
            ):
                segment_stamps.append(snapshot.collected_at_ms)
                coverage.append(snapshot.frame.height / len(symbols))
            stamps.extend(segment_stamps)
            gaps.extend(
                (right - left) / 1000
                for left, right in pairwise(segment_stamps)
            )
        minute = minute_coverage(
            self.minute_path,
            symbols,
            self.minute_start,
            self.minute_end,
        )
        write_json(self.run_dir / "minute_quality.json", minute)
        sample_times = [self.start, *(datetime.fromisoformat(row["at"]) for row in samples), self.end]
        observer_gaps = [
            (right - left).total_seconds() for left, right in pairwise(sample_times)
        ]
        initial_sig = self.record["initial_minute"].get("mtime_ns")
        updates = 0
        for row in samples:
            sig = row.get("minute", {}).get("mtime_ns")
            if sig is not None and sig != initial_sig:
                updates += 1
                initial_sig = sig
        initial_sessions = self.record["initial_sessions"]
        last_sessions = self.sessions()
        active_sessions = {
            key: value for key, value in last_sessions.items()
            if (value.get("last_flush_ms") or 0) >= start_ms
            or (value.get("last_drop_ms") or 0) >= start_ms
        }
        losses = sum(max(0, value.get("dropped_rounds", 0)
                         - initial_sessions.get(key, {}).get("dropped_rounds", 0))
                     for key, value in active_sessions.items())
        archive_samples = [
            value for row in samples for value in row.get("sessions", {}).values()
            if (value.get("last_flush_ms") or 0) >= start_ms
        ]
        writes = {
            (key, value["last_flush_ms"]): float(value["last_write_ms"])
            for row in samples for key, value in row.get("sessions", {}).items()
            if value.get("last_flush_ms") and value.get("last_write_ms") is not None
            and start_ms <= value["last_flush_ms"] <= end_ms
        }
        health_samples = [row["health"] for row in samples if "health" in row]
        # Include only shards overlapping this window in storage measurements.
        window_bytes = 0
        for part in manifest["files"]:
            path = self.data_dir / "quote_second" / part["path"]
            meta = pq.read_metadata(path)
            column = meta.schema.names.index("collected_at_ms")
            stats = [meta.row_group(i).column(column).statistics for i in range(meta.num_row_groups)]
            if any(
                min(s.min for s in stats) <= int(segment_end.timestamp() * 1000)
                and max(s.max for s in stats) >= int(segment_start.timestamp() * 1000)
                for _, segment_start, segment_end in self.segments
            ):
                window_bytes += part["size"]
        milestone_results = {}
        for name, value in self.record.get("milestones", {}).items():
            target = datetime.fromisoformat(value)
            nearest = min(
                samples,
                key=lambda row: abs(
                    (datetime.fromisoformat(row["at"]) - target).total_seconds()
                ),
                default=None,
            )
            delta = (
                abs((datetime.fromisoformat(nearest["at"]) - target).total_seconds())
                if nearest is not None
                else None
            )
            milestone_results[name] = {
                "target": value,
                "sample_at": nearest.get("at") if nearest else None,
                "delta_seconds": delta,
                "health": nearest.get("health") if nearest else None,
            }
        config_stable = bool(samples) and all(
            fingerprint(row.get("preferences")) == self.record["config_fingerprint"] for row in samples
        )
        checks = {
            "full_window_observed": bool(samples) and self.record["start_lateness_s"] <= 10
            and max(observer_gaps) <= 15,
            "telemetry_readable": bool(samples) and not any(row["errors"] for row in samples),
            "configuration_stable": config_stable,
            "backend_ready": bool(health_samples) and all(row["ok"] for row in health_samples),
            "second_quote_quality": quality["status"] == "ready",
            "rustdx_source": quality["providers"] == ["rustdx"],
            "archive_no_reported_loss": bool(active_sessions) and losses == 0,
            "archive_no_reported_error": bool(active_sessions)
            and not any(row.get("last_error") for row in [*active_sessions.values(), *archive_samples]),
            "minute_updated_during_window": updates > 0,
            "minute_coverage_and_freshness": minute["coverage"] >= 0.99,
            "minute_keys_and_clock": minute.get("duplicate_keys", 0) == 0 and minute.get("future_rows", 0) == 0,
            "disk_headroom": bool(samples) and all(row.get("free_bytes", 0) >= 1024**3 for row in samples),
        }
        if milestone_results:
            checks["full_day_milestones_observed"] = all(
                value["delta_seconds"] is not None
                and value["delta_seconds"] <= self.record["thresholds"]["max_observer_gap_s"]
                for value in milestone_results.values()
            )
        failures = [key for key, passed in checks.items() if not passed]
        self.record["trading_date_confirmed"] = quality["status"] == "ready"
        report = {
            "status": "succeeded" if not failures else "failed",
            "profile": self.record.get("profile", "open-window"),
            "window_start": self.start.isoformat(), "window_end": self.end.isoformat(),
            "evaluated_at": now.isoformat(), "checks": checks, "failures": failures,
            "thresholds": self.record["thresholds"],
            "expected_symbols": len(symbols), "universe_fingerprint": universe["fingerprint"],
            "config_fingerprint": self.record["config_fingerprint"], "quality": quality,
            "interval_s": {f"p{p}": percentile(gaps, p / 100) for p in (50, 95, 99)},
            "min_capture_coverage": min(coverage, default=0),
            "observer_max_gap_s": max(observer_gaps),
            "archive": {
                "session_drop_delta": losses,
                "session_queue_peak": max((row.get("queue_peak", 0) for row in archive_samples), default=None),
                "sampled_max_backlog": max((
                    row.get("queued_rounds", 0) + row.get("pending_rounds", 0) for row in archive_samples
                ), default=None),
                "sampled_write_ms": {f"p{p}": percentile(list(writes.values()), p / 100) for p in (50, 95, 99)},
                "window_overlapping_shard_bytes": window_bytes,
                "session_ids": sorted(active_sessions),
            },
            "minute": minute, "observed_minute_file_updates": updates,
            "health_samples": health_samples, "milestones": milestone_results,
            "limitations": [
                "Weekday is a candidate; absent live data fails acceptance, never proves a holiday.",
                "Full A-share denominator includes suspended/inactive instruments; no automatic exclusions.",
                "Session counters/queue peaks can include activity outside the window; sampled writes are not every write.",
                "Quote intervals measure completed snapshots, not request latency or per-security receive time.",
                "Minute freshness is checked at the window end; 99% allows up to 1% missing/stale symbols listed separately.",
                "No archive-off control run, exchange tick reconstruction, or guaranteed exchange event time.",
                "Full-day milestones prove observer continuity only; each business task still requires its own terminal evidence.",
            ],
        }
        write_json(self.run_dir / "report.json", report)
        lines = [
            "# 开盘行情与分钟任务并行验收", "",
            f"结果: {report['status']}", f"窗口: {self.start.isoformat()} — {self.end.isoformat()}",
            f"全市场证券: {len(symbols)}; 采样轮数: {quality['captures']}",
            f"轮次间隔 P50/P95/P99 (秒): {report['interval_s']}",
            f"采样缺口: {quality['gap_count']}; 会话丢轮增量: {losses}",
            f"分钟更新观测次数: {updates}; 末端新鲜覆盖率: {minute['coverage']:.2%}",
            f"窗口相交分片: {window_bytes:,} 字节", "",
            "未通过检查: " + (", ".join(failures) if failures else "无"),
            "秒级质量问题: " + (", ".join(quality["issues"]) if quality["issues"] else "无"), "",
            "完整指标、限制和输入指纹见 report.json; 证券列表见 universe.json; 固定分片清单见 manifest.json。",
            "本报告仅验证当前开启归档与分钟任务的并行场景, 不证明归档性能影响或交易所逐笔精度。",
        ]
        atomic_write_text(self.run_dir / "report.md", "\n\n".join(lines) + "\n")
        self.record["report_path"] = str(self.run_dir / "report.json")
        self.finish(report["status"], now, ", ".join(failures) if failures else None)
        return report

    def step(self, now: datetime) -> None:
        if self.record["status"] in TERMINAL_TASK_STATUSES:
            return
        if now < self.start:
            self.persist(now)
            return
        if self.record["status"] == "queued":
            if now >= self.end:
                self.finish("skipped_prerequisite", now, "window elapsed before observer started")
                return
            self.begin(now)
        if self.record["status"] != "running":
            return
        if now >= self.end + timedelta(seconds=SETTLE_SECONDS):
            self.evaluate(now)
        else:
            self.record["phase"] = "settling" if now > self.end else "observing"
            self.sample(now)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--preferences-path", type=Path)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument(
        "--profile",
        choices=("open-window", "full-trading-day"),
        default="open-window",
    )
    parser.add_argument("--health-url", default="http://127.0.0.1:3018/api/health")
    parser.add_argument("--arm", action="store_true", help="create durable queued task and exit")
    parser.add_argument(
        "--daemonize",
        action="store_true",
        help="launch an already armed task in a detached session",
    )
    parser.add_argument("--once", action="store_true", help="one observer step; no blocking wait")
    args = parser.parse_args()
    now = datetime.now(CN_TZ)
    args.run_dir = args.run_dir.resolve()
    if args.arm:
        if args.data_dir is None:
            parser.error("--arm requires --data-dir")
        print(json.dumps(arm(
            args.run_dir,
            args.data_dir,
            args.date or next_candidate(now),
            args.minutes,
            now,
            profile=args.profile,
            preferences_path=args.preferences_path,
        ),
                         ensure_ascii=False, indent=2))
        return
    if args.daemonize:
        print(json.dumps(
            daemonize(args.run_dir, args.health_url),
            ensure_ascii=False,
            indent=2,
        ))
        return
    # macOS/Linux operational observer: prevent two agents from writing one run.
    import fcntl

    with (args.run_dir / "observer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another observer owns this run")
        observer = Observer(args.run_dir, args.health_url)
        observer.recover(now)
        while observer.record["status"] not in TERMINAL_TASK_STATUSES:
            try:
                observer.step(datetime.now(CN_TZ))
            except Exception as exc:
                observer.finish("failed", datetime.now(CN_TZ), f"{type(exc).__name__}: {exc}")
                raise
            if args.once or observer.record["status"] in TERMINAL_TASK_STATUSES:
                break
            remaining = (observer.start - datetime.now(CN_TZ)).total_seconds()
            time.sleep(min(30, max(0.1, remaining)) if remaining > 0 else SAMPLE_SECONDS)
        print(json.dumps(observer.record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
