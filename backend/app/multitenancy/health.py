"""Readiness for shared files, the platform bridge and independent workers."""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

MARKET_DEPENDENCIES = ("databases", "public_files", "platform")
FUNCTION_DEPENDENCIES = {
    "market": MARKET_DEPENDENCIES,
    "research": (*MARKET_DEPENDENCIES, "indicator_cache", "research_worker", "compute_broker"),
    "monitoring": (*MARKET_DEPENDENCIES, "indicator_cache", "realtime_worker", "compute_broker"),
    "paper_trading": (*MARKET_DEPENDENCIES, "indicator_cache", "realtime_worker", "scheduled_worker",
                      "compute_broker"),
    "notifications": ("databases", "notifications_worker"),
    "scheduled": (*MARKET_DEPENDENCIES, "scheduled_worker", "compute_broker"),
}


def function_health(checks: dict) -> dict:
    functions = {}
    for name, dependencies in FUNCTION_DEPENDENCIES.items():
        unavailable = [
            dependency for dependency in dependencies
            if checks.get(dependency, {}).get("status") != "ok"
        ]
        functions[name] = {
            "ready": not unavailable,
            "status": "unavailable" if unavailable else "ok",
            "unavailable_dependencies": unavailable,
        }
    return functions


def worker_fresh(kind: str, healthy: bool, age_seconds: float) -> bool:
    from app.multitenancy.worker_process import MAX_AGE

    max_age = MAX_AGE.get(kind)
    return bool(healthy and max_age is not None and 0 <= age_seconds <= max_age)


class WorkerHealth:
    def __init__(self, db, kind):
        self.db, self.kind = db, kind
        self.owner, self.last = uuid.uuid4().hex, 0
        self.last_renewal = 0

    def record(self, healthy=True, *, progress=True):
        last = self.last if progress else max(self.last, self.last_renewal)
        if healthy and time.monotonic() - last < 5:
            return
        with self.db.control.connection() as conn:
            conn.execute(
                """INSERT INTO control.full_worker_health(cell_id,kind,owner,healthy)
                   VALUES(%s,%s,%s,%s) ON CONFLICT(cell_id,kind) DO UPDATE
                   SET owner=excluded.owner,healthy=excluded.healthy,checked_at=clock_timestamp()""",
                (self.db.settings.id, self.kind, self.owner, healthy),
            )
        if progress:
            self.last = time.monotonic()
            from app.multitenancy.worker_process import record

            record(self.kind, healthy)
        else:
            # Lease renewal must not suppress a later dispatch heartbeat.
            self.last_renewal = time.monotonic()


def readiness(state, data_dir):
    checks = {}
    quotes = getattr(state, "quote_service", None)
    view = quotes.binding_snapshot() if quotes and hasattr(quotes, "binding_snapshot") else None
    snapshot = view.snapshot if view is not None else getattr(quotes, "snapshot", None)
    identity = getattr(state, "account_identity", None)
    try:
        with identity.db.control.connection() as conn:
            workers = conn.execute(
                """SELECT kind,healthy,
                          extract(epoch FROM clock_timestamp()-checked_at) AS age_seconds
                   FROM control.full_worker_health WHERE cell_id=%s""", (identity.db.settings.id,),
            ).fetchall()
        with identity.db.data.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["databases"] = {"status": "ok"}
        found = {row["kind"]: row for row in workers}
        from app.multitenancy.worker_process import MAX_AGE

        for kind in ("research", "realtime", "notifications", "scheduled"):
            row = found.get(kind)
            age = float(row["age_seconds"]) if row is not None else None
            fresh = row is not None and worker_fresh(kind, row["healthy"], age)
            checks[kind + "_worker"] = {
                "status": "ok" if fresh else "error",
                "age_seconds": age,
                "max_age_seconds": MAX_AGE[kind],
            }
    except Exception:
        checks["databases"] = {"status": "error"}
    try:
        if view is not None:
            from app.config import settings
            from app.multitenancy.public_reader import PublicReleaseError, current_release

            diag = {
                "binding_current": False,
                "sync_age_seconds": view.sync["last_checked_age_seconds"],
                "mismatch_age_seconds": view.sync["mismatch_age_seconds"],
                "last_error_kind": view.sync["last_error_kind"],
                "reader_lock_held": bool(view.lease and view.lease.held),
            }
            checks["public_files"] = {"status": "error", **diag}
            if view.repo is None or view.lease is None:
                raise PublicReleaseError()
            release = view.lease.validate()
            target = current_release(settings.data_dir)
            checks["public_files"]["binding_current"] = target == view.repo.store.data_dir
            # Check the current manifest as well when the old binding is in its
            # grace period. A broken current entry must still fail immediately.
            if target != view.repo.store.data_dir:
                from app.multitenancy.public_reader import PublicReleaseLease

                current_lease = PublicReleaseLease.acquire(target)
                try:
                    current_lease.validate()
                finally:
                    current_lease.close()
            if view.sync["last_error_kind"] in {"sync_stalled", "stopped", "current_unavailable"}:
                raise PublicReleaseError(view.sync["last_error_kind"])
            checks["public_files"].update(status="ok", release_id=release["id"])
        else:
            release = json.loads((data_dir / ".public_release.json").read_text())
            checks["public_files"] = {"status": "ok", "release_id": release["id"]}
    except (OSError, ValueError, KeyError) as exc:
        checks.setdefault("public_files", {})["status"] = "error"
        if view is not None:
            checks["public_files"]["last_error_kind"] = getattr(exc, "kind", "invalid_release")
    bridge = quotes.bridge_status() if quotes and hasattr(quotes, "bridge_status") else {}
    connected = bool(
        snapshot
        and (
            bridge.get("connected")
            or time.monotonic() - (view.received_at if view is not None else quotes.received_at) <= 15
        )
    )
    checks["platform"] = {
        "status": "ok" if connected and snapshot.get("platform_ready") else "error",
        "connected": connected,
        **bridge,
    }
    indicators_ready = view.indicators_ready if view is not None else getattr(state, "indicators_ready", False)
    checks["indicator_cache"] = {"status": "ok" if indicators_ready else "error"}
    try:
        healthy = state.compute_executor.health()["ready"]
        checks["compute_broker"] = {"status": "ok" if healthy else "error"}
    except Exception:
        checks["compute_broker"] = {"status": "error"}
    functions = function_health(checks)
    ready = functions["market"]["ready"]
    degraded = any(check["status"] != "ok" for check in checks.values())
    return {
        "status": "error" if not ready else "degraded" if degraded else "ok",
        "ready": ready, "checked_at": datetime.now(UTC).isoformat(),
        "checks": checks, "functions": functions,
    }
