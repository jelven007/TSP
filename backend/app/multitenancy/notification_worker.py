"""Independent full-product notification outbox dispatcher."""
from __future__ import annotations

import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from app.cell.config import CellSettings
from app.cell.database import Databases
from app.cell.scheduler import Scheduler
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.health import WorkerHealth
from app.multitenancy.notifications import Notifications

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO)
    db = Databases(CellSettings())
    db.open()
    notifications = Notifications(db)
    scheduler = Scheduler(db)
    health = WorkerHealth(db, "notifications")
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())

    def process(claim):
        due = datetime.now(UTC) + timedelta(seconds=5)
        try:
            notifications.process(claim["tenant_id"])
            _account, context = notifications.runtime.account(claim["tenant_id"])
            with TenantDocuments(db, context)._transaction() as conn:
                due = Scheduler.next_due(conn, "notifications", 1)
                queued = conn.execute(
                    """SELECT EXISTS(SELECT 1 FROM business.documents WHERE namespace='notification_requests'
                       AND value->>'state'='queued') AS queued""",
                ).fetchone()["queued"]
                if queued:
                    due = datetime.now(UTC)
        except Exception:
            logger.exception("full notification worker deferred")
        finally:
            scheduler.finish(claim, due)

    try:
        capacity = min(8, db.settings.worker_concurrency)
        with ThreadPoolExecutor(max_workers=capacity) as pool:
            while not stopped.is_set():
                claims = scheduler.claim("notifications", capacity)
                health.record()
                if claims:
                    list(pool.map(process, claims))
                else:
                    stopped.wait(.25)
    finally:
        db.close()


if __name__ == "__main__":
    main()
