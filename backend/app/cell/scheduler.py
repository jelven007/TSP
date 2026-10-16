"""Bounded SQL work discovery; never scan the tenant directory on a worker tick."""

import uuid
from datetime import UTC, datetime, timedelta


class Scheduler:
    def __init__(self, db):
        self.db = db

    def claim(self, kind, capacity=None):
        with self.db.data.connection() as conn:
            return conn.execute(
                "SELECT * FROM scheduler.claim(%s,%s,%s,%s)",
                (kind, capacity or self.db.settings.worker_concurrency,
                 max(120, self.db.settings.worker_timeout + 45), uuid.uuid4().hex),
            ).fetchall()

    def finish(self, claim, due_at):
        with self.db.data.connection() as conn:
            return conn.execute(
                "SELECT scheduler.finish(%s,%s,%s,%s,%s) AS completed",
                (claim["tenant_id"], claim["kind"], claim["owner"], claim["revision"], due_at),
            ).fetchone()["completed"]

    @staticmethod
    def next_due(conn, kind, interval):
        now = datetime.now(UTC)
        if kind == "realtime":
            active = conn.execute(
                """SELECT EXISTS(SELECT 1 FROM business.monitor_rules WHERE enabled)
                   OR EXISTS(SELECT 1 FROM business.paper_orders WHERE status='queued')
                   OR EXISTS(SELECT 1 FROM business.trade_automations WHERE enabled) AS active"""
            ).fetchone()["active"]
            return now + timedelta(seconds=interval) if active else None
        if kind == "strategies":
            row = conn.execute(
                """SELECT min(CASE WHEN status='queued' OR lease_until IS NULL THEN now() ELSE lease_until END) AS due
                   FROM business.strategy_jobs WHERE status IN ('queued','running','cancelling')"""
            ).fetchone()
            return row["due"]
        if kind == "notifications":
            return conn.execute(
                """SELECT min(CASE WHEN status='sending' THEN lease_until ELSE due_at END) AS due
                   FROM business.notification_outbox WHERE status IN ('queued','sending')"""
            ).fetchone()["due"]
        if kind == "research":
            return conn.execute(
                """SELECT min(CASE WHEN state='queued' THEN now() ELSE lease_until END) AS due
                   FROM business.research_jobs WHERE state IN ('queued','running','cancelling')"""
            ).fetchone()["due"]
        active = conn.execute(
            """SELECT EXISTS(SELECT 1 FROM business.trade_automations WHERE enabled)
               OR EXISTS(SELECT 1 FROM business.trade_intents WHERE status='queued') AS active"""
        ).fetchone()["active"]
        return now + timedelta(seconds=2) if active else None
