"""Transactional notification outbox, with bounded delivery and receiver idempotency."""

import json
import os
import smtplib
import ssl
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from app.cell.context import AccessError, Role
from app.cell.identity import Identity
from app.cell.watchlist import Watchlist


class DeliveryError(Exception):
    def __init__(self, code: str, retryable: bool):
        self.code, self.retryable = code, retryable


class NotificationTransport:
    def __init__(self, settings):
        self.settings = settings

    def validate_destination(self, kind: str, destination: str) -> None:
        if kind == "email":
            if any(c in destination for c in "\r\n,;") or destination.count("@") != 1:
                raise AccessError(422, "invalid_notification_destination")
            if not self.settings.smtp_host or not self.settings.smtp_from:
                raise AccessError(409, "email_delivery_not_configured")
            return
        parsed = urlsplit(destination)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        # Only operator-approved origins; no tenant-selected internal hosts or redirects.
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or (
            origin not in self.settings.webhook_origins
        ):
            raise AccessError(422, "webhook_origin_not_allowed")

    def send(self, channel: dict, event: dict, delivery_id: str) -> None:
        # A socket timeout only bounds an individual read. Kill a slow/trickling
        # receiver at a wall deadline so it cannot monopolize a worker/authorization lock.
        payload = {
            "channel": channel, "event": event, "id": delivery_id,
            "settings": {
                "id": self.settings.id, "control_dsn": "", "data_dsn": "",
                "smtp_host": self.settings.smtp_host, "smtp_port": self.settings.smtp_port,
                "smtp_from": self.settings.smtp_from, "smtp_user": self.settings.smtp_user,
                "smtp_password": self.settings.smtp_password.get_secret_value(),
                "smtp_security": self.settings.smtp_security, "webhook_origins": self.settings.webhook_origins,
            },
        }
        environment = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "LANG", "PYTHONPATH"}}
        try:
            result = subprocess.run(
                [sys.executable, "-m", "app.cell.notify_send"], input=json.dumps(payload, default=str),
                text=True, capture_output=True, timeout=8, env=environment, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeliveryError("notification_timeout", True) from exc
        if result.returncode:
            raise DeliveryError("notification_delivery_failed", True)
        outcome = json.loads(result.stdout)
        if outcome.get("error"):
            raise DeliveryError(outcome["error"], outcome["retryable"])

    def send_direct(self, channel: dict, event: dict, delivery_id: str) -> None:
        try:
            self.validate_destination(channel["kind"], channel["destination"])
        except AccessError as exc:
            raise DeliveryError(exc.code, False) from exc
        if channel["kind"] == "webhook":
            try:
                with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client, client.stream("POST", channel["destination"], json={
                        "id": delivery_id, "event": event,
                    }, headers={"Idempotency-Key": delivery_id}) as response:
                    if 200 <= response.status_code < 300:
                        return
                    raise DeliveryError("notification_http_failed", response.status_code in {408, 425, 429} or response.status_code >= 500)
            except httpx.HTTPError as exc:
                raise DeliveryError("notification_connection_failed", True) from exc
        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = channel["destination"]
        message["Subject"] = "TSP 工作空间提醒"
        message["Message-ID"] = f"<{delivery_id}@tsp-notifications>"
        message.set_content(event.get("message", "工作空间有新的提醒"))
        try:
            client_type = smtplib.SMTP_SSL if self.settings.smtp_security == "tls" else smtplib.SMTP
            options = {"context": ssl.create_default_context()} if self.settings.smtp_security == "tls" else {}
            with client_type(self.settings.smtp_host, self.settings.smtp_port, timeout=5, **options) as client:
                if self.settings.smtp_security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                if self.settings.smtp_user:
                    client.login(self.settings.smtp_user, self.settings.smtp_password.get_secret_value())
                client.send_message(message)
        except smtplib.SMTPResponseException as exc:
            raise DeliveryError("notification_smtp_failed", exc.smtp_code < 500) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError("notification_connection_failed", True) from exc


class Notifications:
    def __init__(self, db, transport=None):
        self.db = db
        self.identity = Identity(db)
        self.transport = transport or NotificationTransport(db.settings)

    def channels(self, ctx):
        with self.db.business_transaction(ctx, permission="alerts:read") as conn:
            return conn.execute(
                "SELECT id,name,kind,destination,enabled,version FROM business.notification_channels ORDER BY id"
            ).fetchall()

    def save(self, ctx, body):
        if ctx.role not in {Role.OWNER, Role.ADMIN} or ctx.token_id:
            raise AccessError(403, "notification_admin_required")
        self.transport.validate_destination(body["kind"], body["destination"])
        with self.db.business_transaction(ctx, write=True, permission="alerts:write") as conn:
            channel_id = body.get("id") or uuid.uuid4().hex
            existing = conn.execute("SELECT version FROM business.notification_channels WHERE id=%s", (channel_id,)).fetchone()
            if body.get("id") and not existing:
                raise AccessError(404, "notification_channel_not_found")
            if existing and body.get("version") != existing["version"]:
                raise AccessError(409, "notification_version_stale")
            if not existing and conn.execute("SELECT count(*) AS n FROM business.notification_channels").fetchone()["n"] >= 5:
                raise AccessError(429, "notification_channel_limit")
            row = conn.execute(
                """INSERT INTO business.notification_channels
                   (tenant_id,id,name,kind,destination,enabled,user_id,policy_version,routing_epoch)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,id) DO UPDATE SET
                   name=excluded.name,kind=excluded.kind,destination=excluded.destination,
                   enabled=excluded.enabled,version=business.notification_channels.version+1,
                   user_id=excluded.user_id,policy_version=excluded.policy_version,routing_epoch=excluded.routing_epoch
                   RETURNING id,name,kind,destination,enabled,version""",
                (ctx.tenant_id, channel_id, body["name"], body["kind"], body["destination"], body["enabled"],
                 ctx.user_id, ctx.policy_version, ctx.routing_epoch),
            ).fetchone()
            conn.execute(
                """UPDATE business.notification_outbox SET status='cancelled',lease_until=NULL
                   WHERE channel_id=%s AND status IN ('queued','sending')""", (channel_id,),
            )
            Watchlist._audit(conn, ctx, "notification.channel.save", channel_id)
            return row

    def deliveries(self, ctx):
        with self.db.business_transaction(ctx, permission="alerts:read") as conn:
            return conn.execute(
                """SELECT id,channel_id,alert_id,status,attempts,error,created_at,sent_at
                   FROM business.notification_outbox ORDER BY created_at DESC LIMIT 100"""
            ).fetchall()

    def retry(self, ctx, delivery_id):
        if ctx.role not in {Role.OWNER, Role.ADMIN} or ctx.token_id:
            raise AccessError(403, "notification_admin_required")
        with self.db.business_transaction(ctx, write=True, permission="alerts:write") as conn:
            row = conn.execute(
                """UPDATE business.notification_outbox o SET status='queued',attempts=0,error=NULL,due_at=now()
                   FROM business.notification_channels c WHERE o.id=%s AND o.channel_id=c.id AND c.enabled
                   AND o.channel_version=c.version AND o.status='dead' RETURNING o.id""", (delivery_id,),
            ).fetchone()
            if not row:
                raise AccessError(409, "notification_not_retryable")
            Watchlist._audit(conn, ctx, "notification.retry", delivery_id)

    def process(self, tenant_id):
        with self.identity.worker_context(tenant_id) as (ctx, _), self.db.business_transaction(
            ctx, write=True, permission="alerts:write"
        ) as conn:
            row = conn.execute(
                """SELECT * FROM business.notification_outbox
                   WHERE (status='queued' AND due_at<=now()) OR (status='sending' AND lease_until<now())
                   ORDER BY due_at,id FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return 0
            if row["attempts"] >= 6:
                conn.execute(
                    "UPDATE business.notification_outbox SET status='dead',error='notification_attempts_exhausted' WHERE id=%s",
                    (row["id"],),
                )
                return 1
            epoch = row["execution_epoch"] + 1
            conn.execute(
                """UPDATE business.notification_outbox SET status='sending',attempts=attempts+1,
                   execution_epoch=%s,lease_until=now()+interval '30 seconds' WHERE id=%s""", (epoch, row["id"]),
            )
            channel = conn.execute("SELECT * FROM business.notification_channels WHERE id=%s", (row["channel_id"],)).fetchone()
            event = conn.execute("SELECT event FROM business.alerts WHERE id=%s", (row["alert_id"],)).fetchone()["event"]
        error, status = None, "sent"
        try:
            # Revocation waits for this bounded external operation, as with an authorized API request.
            with self.identity.worker_context(tenant_id) as (ctx, control):
                self.identity.authorize_background(control, ctx, channel, {"alerts:write"}, admin=True)
                with self.db.business_transaction(ctx, permission="alerts:write") as conn:
                    current = conn.execute("SELECT * FROM business.notification_channels WHERE id=%s FOR SHARE", (channel["id"],)).fetchone()
                    if not current["enabled"] or current["version"] != row["channel_version"]:
                        raise AccessError(403, "notification_channel_changed")
                    attempt = conn.execute(
                        "SELECT status,execution_epoch,lease_until FROM business.notification_outbox WHERE id=%s FOR SHARE",
                        (row["id"],),
                    ).fetchone()
                    if attempt["status"] != "sending" or attempt["execution_epoch"] != epoch or attempt["lease_until"] <= datetime.now(UTC):
                        return 0
                    self.transport.send(channel, json.loads(json.dumps(event, default=str)), row["id"])
        except AccessError as exc:
            error, status = exc.code, "cancelled"
        except DeliveryError as exc:
            error = exc.code
            status = "queued" if exc.retryable and row["attempts"] + 1 < 6 else "dead"
        except Exception:
            error, status = "notification_delivery_failed", "dead"
        with self.identity.worker_context(tenant_id) as (ctx, _), self.db.business_transaction(
            ctx, write=True, permission="alerts:write"
        ) as conn:
            conn.execute(
                """UPDATE business.notification_outbox SET status=%s,error=%s,lease_until=NULL,
                   sent_at=%s,due_at=%s WHERE id=%s AND status='sending' AND execution_epoch=%s
                   AND lease_until>now()""",
                (status, error, datetime.now(UTC) if status == "sent" else None,
                 datetime.now(UTC) + timedelta(seconds=min(300, 5 * 2 ** row["attempts"])), row["id"], epoch),
            )
        return 1
