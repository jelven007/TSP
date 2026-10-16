"""Durable tenant events with bounded replay and one dispatcher per API process."""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from app.cell.context import AccessError
from app.multitenancy.access import verify_live_access
from app.multitenancy.documents import TenantDocuments, json_value

logger = logging.getLogger(__name__)
_FULL_SWEEP_SECONDS = 30.0
_API_PRIVATE_EVENT_SCOPES = {
    "strategy_alert": "read:analysis",
    "strategy_results_updated": "read:analysis",
    "review_progress": "read:analysis",
}


def private_event_visible(context, event) -> bool:
    """API credentials receive only private event kinds covered by their scopes."""
    scopes = context.token_scopes
    if scopes is None or event["kind"] == "events_reset":
        return True
    required = _API_PRIVATE_EVENT_SCOPES.get(event["kind"])
    return required is not None and required in scopes


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
            # Authoritative state remains available through REST. Tell a slow
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
        self.wakeups: queue.Queue[str] = queue.Queue(maxsize=4096)
        self.pool = ThreadPoolExecutor(max_workers=min(4, db.settings.pool_size))
        self.thread = threading.Thread(target=self._loop, daemon=True, name="tenant-events")
        self.listener_connected = False
        self.poll_runs = 0
        self.tenant_reads = 0
        self.notifications = 0
        self.full_sweeps = 0
        self.last_event_at = None

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
        self.notify(documents.context.tenant_id)
        return sub

    def unsubscribe(self, sub):
        with self.lock:
            self.subscriptions.discard(sub)

    def notify(self, tenant_id):
        with suppress(queue.Full):
            self.wakeups.put_nowait(tenant_id)

    def _drain_wakeups(self):
        tenants = set()
        while True:
            try:
                tenants.add(self.wakeups.get_nowait())
            except queue.Empty:
                return tenants

    def poll(self, tenant_ids=None):
        with self.lock:
            subscribed = list(self.subscriptions)
        tenants = {}
        for sub in subscribed:
            tenant_id = sub.documents.context.tenant_id
            if tenant_ids is None or tenant_id in tenant_ids:
                tenants.setdefault(tenant_id, []).append(sub)

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

        self.poll_runs += 1
        self.tenant_reads += len(tenants)
        list(self.pool.map(read, tenants.values()))

    def _loop(self):
        # LISTEN only wakes readers. SQL remains the durable source, and the
        # full sweep recovers missed notifications or a listener reconnect.
        import psycopg

        next_sweep = time.monotonic() + _FULL_SWEEP_SECONDS
        while not self.stop.is_set():
            try:
                with psycopg.connect(
                    self.db.settings.data_dsn.get_secret_value(),
                    autocommit=True,
                    connect_timeout=5,
                ) as conn:
                    conn.execute("LISTEN tsp_full_events")
                    self.listener_connected = True
                    while not self.stop.is_set():
                        tenants = self._drain_wakeups()
                        for notification in conn.notifies(timeout=.5, stop_after=256):
                            tenants.add(notification.payload)
                            self.notifications += 1
                            self.last_event_at = time.time()
                        if tenants:
                            self.poll(tenants)
                        if time.monotonic() >= next_sweep:
                            self.poll()
                            self.full_sweeps += 1
                            next_sweep = time.monotonic() + _FULL_SWEEP_SECONDS
            except Exception:
                logger.warning("tenant event listener unavailable")
                # During listener failure, temporarily poll at low frequency so
                # durable private events still progress until LISTEN recovers.
                self.poll()
                self.stop.wait(1)
            finally:
                self.listener_connected = False

    def status(self):
        return {
            "listener_connected": self.listener_connected,
            "subscriptions": len(self.subscriptions),
            "poll_runs": self.poll_runs,
            "tenant_reads": self.tenant_reads,
            "notifications": self.notifications,
            "full_sweeps": self.full_sweeps,
            "last_event_at": self.last_event_at,
        }


def sse_event(event):
    return {
        "id": str(event["sequence"]), "event": event["kind"],
        "data": json.dumps(event["payload"], ensure_ascii=False),
    }


async def stream(request, claims=None):
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
            if claims is None:
                await run_in_threadpool(verify_live_access, request)
            else:
                revocations = getattr(request.app.state, "stream_revocations", None)
                if revocations is not None:
                    revocations.ensure_active(claims)
                elif claims.expires_at <= time.time():
                    raise AccessError(401, "流授权已过期,请重新连接")
            if private:
                for event in private.pop():
                    if private_event_visible(context, event):
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
