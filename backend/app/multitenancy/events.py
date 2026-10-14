"""Durable tenant events with bounded replay and one dispatcher per API process."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from app.cell.context import AccessError
from app.multitenancy.access import verify_live_access
from app.multitenancy.documents import TenantDocuments, json_value


def append_event(conn, tenant_id, kind, payload):
    row = conn.execute(
        """INSERT INTO business.full_events(tenant_id,kind,payload)
           VALUES(%s,%s,%s) RETURNING sequence""", (tenant_id, kind, Jsonb(json_value(payload))),
    ).fetchone()
    conn.execute(
        """DELETE FROM business.full_events WHERE created_at<now()-interval '1 day'
           OR sequence IN (SELECT sequence FROM business.full_events ORDER BY sequence DESC OFFSET 5000)""",
    )
    conn.execute("SELECT pg_notify('tsp_full_events',%s)", (tenant_id,))
    return row["sequence"]


class EventLog:
    def __init__(self, documents: TenantDocuments):
        self.documents = documents

    def head(self):
        with self.documents._transaction() as conn:
            return conn.execute(
                "SELECT coalesce(max(sequence),0) AS head FROM business.full_events",
            ).fetchone()["head"]

    def since(self, cursor, limit=200):
        with self.documents._transaction() as conn:
            return conn.execute(
                """SELECT sequence,kind,payload FROM business.full_events WHERE sequence>%s
                   ORDER BY sequence LIMIT %s""", (cursor, min(200, limit)),
            ).fetchall()


class EventSubscription:
    def __init__(self, documents, cursor):
        self.documents, self.cursor = documents, cursor
        self.pending = queue.Queue(maxsize=512)

    def push(self, event):
        try:
            self.pending.put_nowait(event)
        except queue.Full:
            # History remains available through REST. Explicitly tell a slow
            # client to refresh before resuming from the latest event.
            while not self.pending.empty():
                try:
                    self.pending.get_nowait()
                except queue.Empty:
                    break
            self.pending.put_nowait({"sequence": event["sequence"], "kind": "events_reset", "payload": {}})
        self.cursor = event["sequence"]

    def pop(self):
        events = []
        while True:
            try:
                events.append(self.pending.get_nowait())
            except queue.Empty:
                return events


class EventHub:
    def __init__(self, db):
        self.db = db
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.subscriptions = set()
        self.pool = ThreadPoolExecutor(max_workers=min(4, db.settings.pool_size))
        self.thread = threading.Thread(target=self._loop, daemon=True, name="tenant-events")

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.pool.shutdown(wait=True, cancel_futures=True)

    def subscribe(self, documents, last_id=None):
        log = EventLog(documents)
        head = log.head()
        try:
            cursor = int(last_id) if last_id else head
        except ValueError:
            cursor = head
        sub = EventSubscription(documents, max(0, min(cursor, head)))
        with self.lock:
            self.subscriptions.add(sub)
        return sub

    def unsubscribe(self, sub):
        with self.lock:
            self.subscriptions.discard(sub)

    def poll(self):
        with self.lock:
            subscribed = list(self.subscriptions)
        tenants = {}
        for sub in subscribed:
            tenants.setdefault(sub.documents.context.tenant_id, []).append(sub)

        def read(subscribers):
            cursor = min(sub.cursor for sub in subscribers)
            try:
                events = EventLog(subscribers[0].documents).since(cursor)
            except Exception:
                return
            for sub in subscribers:
                for event in events:
                    if event["sequence"] > sub.cursor:
                        sub.push(event)

        list(self.pool.map(read, tenants.values()))

    def _loop(self):
        # One SQL reader per tenant regardless of tabs. Live streams separately
        # re-authorize before delivering either private or public events.
        while not self.stop.wait(.5):
            self.poll()


def sse_event(event):
    return {
        "id": str(event["sequence"]), "event": event["kind"],
        "data": json.dumps(event["payload"], ensure_ascii=False),
    }


async def stream(request):
    quotes = request.app.state.quote_service
    public = quotes.subscribe()
    hub = request.app.state.tenant_event_hub
    context = getattr(request.state, "tenant_context", None)
    private = None
    try:
        if context:
            documents = TenantDocuments(request.app.state.account_identity.db, context)
            private = await run_in_threadpool(
                hub.subscribe, documents,
                request.headers.get("last-event-id") or request.query_params.get("last_event_id"),
            )
        while True:
            if await request.is_disconnected():
                return
            await run_in_threadpool(verify_live_access, request)
            if private:
                for event in private.pop():
                    yield sse_event(event)
            data = public.pop()
            for flag, kind in (("quote_updated", "quotes_updated"), ("depth_updated", "depth_updated")):
                if data[flag]:
                    yield {"event": kind, "data": json.dumps({"ts": int(time.time() * 1000)})}
            # Public subscriptions deliberately discard all private channels.
            await asyncio.sleep(1)
    except AccessError:
        yield {"event": "access_revoked", "data": "{}"}
    finally:
        quotes.unsubscribe(public)
        if private:
            hub.unsubscribe(private)
