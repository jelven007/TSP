"""Application liveness and readiness checks."""
from __future__ import annotations

import os
import shutil
import threading
import time as clock_time
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.market_time import cn_now, published_trading_days
from app.services.data_integrity import cached_trading_calendar
from app.services.data_release import collect_dataset_snapshot, release_health
from app.services.provider_audit import audit_provider_routes

_DISK_WARNING_BYTES = 1024 * 1024 * 1024
_DISK_ERROR_BYTES = 100 * 1024 * 1024


class ReadinessMonitor:
    """Dedicated local checks keep HTTP probes out of the business thread pool."""

    def __init__(self, app_state: object, data_dir: Path, *, interval=5, max_age=15):
        self.app_state, self.data_dir = app_state, Path(data_dir)
        self.interval, self.max_age = interval, max_age
        self._result: tuple[float, dict[str, Any]] | None = None
        self._stop = threading.Event()
        self._first_check = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="platform-readiness", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = clock_time.monotonic()
            try:
                payload = readiness(self.app_state, self.data_dir)
            except Exception as exc:
                payload = {
                    "ready": False, "status": "error", "checks": {
                        "readiness_monitor": {"status": "error", "message": str(exc)},
                    },
                }
            if self._stop.is_set():
                return
            self._result = (started, payload)
            self._first_check.set()
            if self._stop.wait(self.interval):
                return

    def snapshot(self) -> dict[str, Any]:
        result = self._result
        if self._stop.is_set():
            reason = "stopped"
        elif result is None:
            reason = "initializing"
        elif clock_time.monotonic() - result[0] > self.max_age:
            reason = "stale"
        else:
            return dict(result[1])
        previous = result[1] if result else {}
        return {
            **previous, "ready": False, "status": "error",
            "checks": {
                **previous.get("checks", {}),
                "readiness_monitor": {"status": "error", "reason": reason},
            },
        }

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)


def readiness_snapshot(app_state: object, data_dir: Path) -> dict[str, Any]:
    monitor = getattr(app_state, "readiness_monitor", None)
    return monitor.snapshot() if monitor is not None else readiness(app_state, data_dir)


def _check_data_dir(data_dir: Path) -> dict[str, Any]:
    exists = data_dir.exists() and data_dir.is_dir()
    writable = exists and os.access(data_dir, os.W_OK)
    if not exists:
        return {"status": "error", "exists": False, "writable": False}
    if not writable:
        return {"status": "error", "exists": True, "writable": False}
    return {"status": "ok", "exists": True, "writable": True}


def _check_disk(data_dir: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(data_dir)
    except OSError as exc:
        return {"status": "error", "message": str(exc)}
    if usage.free < _DISK_ERROR_BYTES:
        status = "error"
    elif usage.free < _DISK_WARNING_BYTES:
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def _check_scheduler(app_state: object) -> dict[str, Any]:
    scheduler = getattr(app_state, "scheduler", None)
    if scheduler is None:
        return {"status": "error", "running": False, "jobs": 0}
    running = bool(getattr(scheduler, "running", False))
    try:
        jobs = len(scheduler.get_jobs())
    except Exception as exc:
        return {"status": "error", "running": running, "jobs": None, "message": str(exc)}
    return {
        "status": "ok" if running else "error",
        "running": running,
        "jobs": jobs,
    }


def _check_data_freshness(data_dir: Path) -> dict[str, Any]:
    snapshot = collect_dataset_snapshot(data_dir)
    now = cn_now()
    today = now.date()
    # 盘后下载、重算留出时间; 18:00 以前仅要求前一个已完成交易日。
    cutoff = today if now.time() >= time(18) else today - timedelta(days=1)
    calendar = cached_trading_calendar()
    covered = bool(calendar and min(calendar) <= cutoff <= max(calendar))
    published = published_trading_days(cutoff.year)
    if published is not None:
        calendar = {day for day in calendar if day.year != cutoff.year} | set(published)
        covered = True
    observed = set(calendar)
    for path in (data_dir / "kline_index_daily").glob("date=*"):
        try:
            if path.is_dir():
                day = date.fromisoformat(path.name.removeprefix("date="))
                # 已公布年份不能被历史误写的节假日目录推翻。
                if published is None or day.year != cutoff.year or day in published:
                    observed.add(day)
        except ValueError:
            continue
    completed = {day for day in observed if day <= cutoff}
    reference = max(completed, default=None)
    latest_text = snapshot["stock_enriched"].get("latest_partition")
    latest = date.fromisoformat(latest_text) if latest_text else None
    stale: list[str] = []
    missing: list[str] = []
    future: list[str] = []
    for name in ("stock_daily", "stock_enriched", "stock_minute"):
        value = snapshot[name].get("latest_partition")
        day = date.fromisoformat(value) if value else None
        if day is None:
            missing.append(name)
        elif day > today:
            future.append(name)
        elif reference is not None and day < reference:
            stale.append(name)
    return {
        "status": "warning" if stale or missing or future or not covered else "ok",
        "stock_daily_latest": snapshot["stock_daily"].get("latest_partition"),
        "stock_enriched_latest": latest_text,
        "stock_minute_latest": snapshot["stock_minute"].get("latest_partition"),
        "auction_snapshot_latest": snapshot["auction_snapshot"].get("latest_partition"),
        "calendar_lag_days": (today - latest).days if latest else None,
        "observed_trading_lag": sum(day > latest for day in completed) if latest else None,
        "reference_trading_date": reference.isoformat() if reference else None,
        "calendar_coverage": "complete" if covered else "unknown",
        "freshness_cutoff": cutoff.isoformat(),
        "stale_datasets": stale,
        "missing_datasets": missing,
        "future_datasets": future,
        "message": (
            "数据落后于已确认的交易日期" if stale
            else "交易日历覆盖不足, 无法确认最新应有分区" if not covered
            else None
        ),
    }


def _check_task_state() -> dict[str, Any]:
    try:
        from app.services.pipeline_jobs import job_store

        jobs = job_store.list_recent(limit=20)
        active_id = job_store.active_id()
    except Exception as exc:
        return {
            "status": "warning",
            "active_id": None,
            "last_pipeline": None,
            "message": str(exc),
        }

    last_pipeline = next(
        (
            job
            for job in jobs
            if job.get("kind") == "daily_pipeline"
            or "daily_days" in (job.get("result") or {})
        ),
        None,
    )
    failed = (
        last_pipeline is not None
        and last_pipeline.get("status") in {"failed", "interrupted"}
    )
    return {
        "status": "warning" if failed else "ok",
        "active_id": active_id,
        "last_pipeline": (
            {
                "id": last_pipeline.get("id") or last_pipeline.get("job_id"),
                "status": last_pipeline.get("status"),
                "finished_at": last_pipeline.get("finished_at"),
            }
            if last_pipeline is not None
            else None
        ),
    }


def _safe_check(check: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return check()
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "error_type": type(exc).__name__,
        }


def readiness(app_state: object, data_dir: Path) -> dict[str, Any]:
    """Evaluate local dependencies without calling external providers."""
    repository_ready = getattr(app_state, "repo", None) is not None
    checks = {
        "repository": {
            "status": "ok" if repository_ready else "error",
            "initialized": repository_ready,
        },
        "data_dir": _safe_check(lambda: _check_data_dir(Path(data_dir))),
        "disk": _safe_check(lambda: _check_disk(Path(data_dir))),
        "scheduler": _safe_check(lambda: _check_scheduler(app_state)),
        "providers": _safe_check(audit_provider_routes),
        "data_freshness": _safe_check(lambda: _check_data_freshness(Path(data_dir))),
        "tasks": _safe_check(_check_task_state),
        "data_release": _safe_check(lambda: release_health(Path(data_dir))),
    }
    if not bool(getattr(app_state, "indicators_ready", True)):
        checks["indicator_cache"] = {
            "status": "warning",
            "ready": False,
        }

    statuses = [str(check.get("status") or "error") for check in checks.values()]
    ready = "error" not in statuses
    status = "ok" if ready and "warning" not in statuses else "degraded"
    if not ready:
        status = "error"
    return {
        "status": status,
        "ready": ready,
        "checked_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "checks": checks,
    }
