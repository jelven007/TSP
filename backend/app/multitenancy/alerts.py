"""Tenant-scoped original alert history; durable events replace global broadcasts."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from psycopg.types.json import Jsonb

from app.multitenancy.documents import TenantDocuments, json_value

MAX_DAYS = 7
MAX_RECORDS = 5000


def append_in_transaction(conn, tenant_id: str, events: list[dict], *, channels=None) -> list[dict]:
    inserted = []
    for event in events:
        value = json_value(event)
        event_id = str(value.get("id") or uuid.uuid4().hex)
        value["id"] = event_id
        dedupe = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        row = conn.execute(
            """INSERT INTO business.alerts(tenant_id,id,dedupe_key,event)
               VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event""",
            (tenant_id, event_id, dedupe, Jsonb(value)),
        ).fetchone()
        if row:
            inserted.append(row["event"])
            from app.multitenancy.events import append_event

            append_event(conn, tenant_id, "strategy_alert", {
                "alerts": [{key: item for key, item in row["event"].items() if key != "content"}],
            })
            rule = conn.execute(
                "SELECT definition FROM business.monitor_rules WHERE id=%s", (value.get("rule_id"),),
            ).fetchone()
            preferences = conn.execute(
                "SELECT value FROM business.documents WHERE namespace='notification_preferences' AND id='default'",
            ).fetchone()
            channel_ids = channels if channels is not None else (
                rule["definition"].get("webhook_channels", []) if rule else
                (preferences["value"].get("default_channels", []) if preferences else [])
            )
            for channel in conn.execute(
                "SELECT id,version FROM business.notification_channels WHERE enabled AND id=ANY(%s)",
                (channel_ids,),
            ).fetchall():
                delivery_id = hashlib.sha256(f"{event_id}:{channel['id']}:{channel['version']}".encode()).hexdigest()
                conn.execute(
                    """INSERT INTO business.notification_outbox
                       (tenant_id,id,channel_id,channel_version,alert_id) VALUES(%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (tenant_id, delivery_id, channel["id"], channel["version"], event_id),
                )
    conn.execute(
        """DELETE FROM business.alerts a WHERE
           (created_at < now()-interval '7 days'
            OR id IN (SELECT id FROM business.alerts ORDER BY created_at DESC,id DESC OFFSET 5000))
           AND NOT EXISTS(SELECT 1 FROM business.notification_outbox o
                          WHERE o.alert_id=a.id AND o.status IN ('queued','sending'))""",
    )
    return inserted


class Alerts:
    def __init__(self, documents: TenantDocuments):
        self.documents = documents

    def append_many(self, events):
        with self.documents.db.business_transaction(
            self.documents.context, write=True, permission="alerts:write",
        ) as conn:
            return append_in_transaction(conn, self.documents.context.tenant_id, events)

    def list_recent(self, days=MAX_DAYS, limit=MAX_RECORDS, source=None, type=None):
        cutoff = int((time.time() - days * 86400) * 1000)
        with self.documents.db.business_transaction(
            self.documents.context, permission="alerts:read",
        ) as conn:
            return [row["event"] for row in conn.execute(
                """SELECT event FROM business.alerts WHERE (event->>'ts')::numeric >= %s
                   AND (%s::text IS NULL OR event->>'source'=%s)
                   AND (%s::text IS NULL OR event->>'type'=%s)
                   ORDER BY (event->>'ts')::numeric DESC,created_at DESC,id DESC LIMIT %s""",
                (cutoff, source, source, type, type, min(MAX_RECORDS, max(0, limit))),
            ).fetchall()]

    def clear(self):
        with self.documents.db.business_transaction(
            self.documents.context, write=True, permission="alerts:write",
        ) as conn:
            return conn.execute("DELETE FROM business.alerts").rowcount

    def delete_one(self, ts):
        with self.documents.db.business_transaction(
            self.documents.context, write=True, permission="alerts:write",
        ) as conn:
            return conn.execute(
                """DELETE FROM business.alerts WHERE id=(SELECT id FROM business.alerts
                   WHERE (event->>'ts')::numeric=%s ORDER BY created_at,id LIMIT 1)""", (ts,),
            ).rowcount > 0

    def count(self):
        with self.documents.db.business_transaction(
            self.documents.context, permission="alerts:read",
        ) as conn:
            return conn.execute("SELECT count(*) AS n FROM business.alerts").fetchone()["n"]
