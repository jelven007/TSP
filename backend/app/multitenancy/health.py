"""Readiness for shared files, the platform bridge and independent workers."""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime


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
        release = json.loads((data_dir / ".public_release.json").read_text())
        checks["public_files"] = {"status": "ok", "release_id": release["id"]}
    except (OSError, ValueError, KeyError):
        checks["public_files"] = {"status": "error"}
    quotes = getattr(state, "quote_service", None)
    snapshot = getattr(quotes, "snapshot", None)
    bridge = quotes.bridge_status() if quotes and hasattr(quotes, "bridge_status") else {}
    connected = bool(
        snapshot
        and (
            bridge.get("connected")
            or time.monotonic() - quotes.received_at <= 15
        )
    )
    checks["platform"] = {
        "status": "ok" if connected and snapshot.get("platform_ready") else "error",
        "connected": connected,
        **bridge,
    }
    checks["indicator_cache"] = {"status": "ok" if getattr(state, "indicators_ready", False) else "error"}
    try:
        healthy = state.compute_executor.health()["ready"]
        checks["compute_broker"] = {"status": "ok" if healthy else "error"}
    except Exception:
        checks["compute_broker"] = {"status": "error"}
    ready = all(check["status"] == "ok" for check in checks.values())
    return {"status": "ok" if ready else "error", "ready": ready,
            "checked_at": datetime.now(UTC).isoformat(), "checks": checks}
