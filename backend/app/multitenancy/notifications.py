"""Full-product notification configuration and durable, authorized delivery."""
# ruff: noqa: RUF001 -- localized business messages.
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, SecretStr

from app.cell.context import AccessError, Role
from app.cell.notifications import DeliveryError, NotificationTransport
from app.multitenancy.alerts import append_in_transaction
from app.multitenancy.documents import TenantDocuments, private_storage
from app.multitenancy.research import authorize
from app.multitenancy.runtime import FullWorker

router = APIRouter(prefix="/api/monitor-rules/notifications", tags=["notifications"])


class ChannelRequest(BaseModel):
    id: str | None = None
    version: int | None = None
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(pattern="^(email|webhook)$")
    destination: str = Field(min_length=1, max_length=2048)
    enabled: bool = False


class PreferencesRequest(BaseModel):
    default_channels: list[str] = Field(default_factory=list, max_length=5)
    review_channels: list[str] = Field(default_factory=list, max_length=5)
    review_push_mode: str = Field(default="manual", pattern="^(manual|auto)$")

class ReviewScheduleRequest(BaseModel):
    enabled: bool = False
    hour: int = Field(default=15, ge=15, le=23)
    minute: int = Field(default=40, ge=0, le=59)

class FullNotificationTransport(NotificationTransport):
    def configuration(self):
        from app.services.email_adapter import get_registration_smtp

        config, password = get_registration_smtp("")
        if not config.get("host"):
            return self.settings
        return self.settings.model_copy(update={
            "smtp_host": config["host"], "smtp_port": config.get("port", 465),
            "smtp_from": config.get("from_address") or config.get("username", ""),
            "smtp_user": config.get("username", ""),
            "smtp_password": SecretStr(password),
            "smtp_security": "tls" if config.get("security") == "ssl" else config.get("security", "tls"),
        })

    def validate_destination(self, kind, destination):
        NotificationTransport(self.configuration()).validate_destination(kind, destination)

    def send(self, channel, event, delivery_id):
        config = self.configuration()
        payload = {
            "channel": channel, "event": event, "id": delivery_id,
            "settings": {key: getattr(config, key) for key in (
                "smtp_host", "smtp_port", "smtp_from", "smtp_user", "smtp_security", "webhook_origins",
            )},
        }
        payload["settings"]["smtp_password"] = config.smtp_password.get_secret_value()
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "SYSTEMROOT", "LANG", "PYTHONPATH"}}
        try:
            result = subprocess.run(
                [sys.executable, "-m", "app.multitenancy.notify_send"],
                input=json.dumps(payload, default=str), text=True, capture_output=True,
                timeout=8, env=environment, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeliveryError("notification_timeout", True) from exc
        if result.returncode:
            raise DeliveryError("notification_delivery_failed", True)
        outcome = json.loads(result.stdout)
        if outcome.get("error"):
            raise DeliveryError(outcome["error"], outcome["retryable"])


class Notifications:
    def __init__(self, db, transport=None):
        self.db = db
        self.transport = transport or FullNotificationTransport(db.settings)
        self.runtime = FullWorker(db, None, None)
        self.identity = self.runtime.identity

    def channels(self, documents):
        with documents._transaction() as conn:
            return conn.execute(
                "SELECT id,name,kind,destination,enabled,version FROM business.notification_channels ORDER BY id",
            ).fetchall()

    def save(self, documents, account, body):
        context = documents.context
        if context.role not in {Role.OWNER, Role.ADMIN} or context.token_id:
            raise AccessError(403, "无权修改通知渠道")
        if body["kind"] == "email" and account["role"] != "admin" and body["destination"] != account["email"]:
            raise AccessError(403, "通知邮箱必须是当前账户邮箱")
        try:
            self.transport.validate_destination(body["kind"], body["destination"])
        except AccessError as exc:
            messages = {
                "email_delivery_not_configured": "平台邮件服务尚未配置，请联系管理员",
                "webhook_origin_not_allowed": "该 Webhook 地址尚未获管理员允许",
            }
            raise AccessError(exc.status, messages.get(exc.code, exc.code)) from exc
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            conn = unit.connection
            item_id = body.get("id") or uuid.uuid4().hex
            current = conn.execute(
                "SELECT version FROM business.notification_channels WHERE id=%s", (item_id,),
            ).fetchone()
            if body.get("id") and not current:
                raise AccessError(404, "通知渠道不存在")
            if current and current["version"] != body.get("version"):
                raise AccessError(409, "通知渠道已更新")
            if not current and conn.execute(
                "SELECT count(*) AS n FROM business.notification_channels",
            ).fetchone()["n"] >= 5:
                raise AccessError(429, "通知渠道数量已达上限")
            conn.execute(
                """INSERT INTO business.notification_channels
                   (tenant_id,id,name,kind,destination,enabled,user_id,auth_version,policy_version,routing_epoch)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,id) DO UPDATE SET
                   name=excluded.name,kind=excluded.kind,destination=excluded.destination,enabled=excluded.enabled,
                   user_id=excluded.user_id,auth_version=excluded.auth_version,
                   policy_version=excluded.policy_version,routing_epoch=excluded.routing_epoch,
                   version=business.notification_channels.version+1""",
                (context.tenant_id, item_id, body["name"], body["kind"], body["destination"], body["enabled"],
                 context.user_id, account["auth_version"], context.policy_version, context.routing_epoch),
            )
            conn.execute(
                """UPDATE business.notification_outbox SET status='cancelled',lease_until=NULL
                   WHERE channel_id=%s AND status IN ('queued','sending')""", (item_id,),
            )
        return self.channels(documents)

    def consume_requests(self, tenant_id):
        account, context = self.runtime.account(tenant_id)
        documents = TenantDocuments(self.db, context)
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            requests = unit.records("notification_requests")
            for row in [row for row in requests if row["value"].get("state") == "queued"][:10]:
                value = row["value"]
                author = replace(context, user_id=value["user_id"])
                try:
                    with authorize(self.identity, author, value["auth_version"]):
                        config = unit.get("notification_preferences", "default", {})
                        channels = config.get("review_channels") or config.get("default_channels", [])
                        append_in_transaction(unit.connection, tenant_id, [{
                            "id": "review:" + row["id"], "source": "review", "type": "report",
                            "ts": int(datetime.now(UTC).timestamp() * 1000), "report_id": value["report_id"],
                            "message": f"{value.get('as_of', '')} 复盘报告已生成",
                            "content": value["content"],
                        }], channels=channels)
                        unit.put("notification_requests", row["id"], {**value, "state": "processed"})
                except AccessError:
                    unit.put("notification_requests", row["id"], {**value, "state": "cancelled"})

    def process(self, tenant_id):
        self.consume_requests(tenant_id)
        account, context = self.runtime.account(tenant_id)
        documents = TenantDocuments(self.db, context)
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            conn = unit.connection
            row = conn.execute(
                """SELECT * FROM business.notification_outbox WHERE
                   (status='queued' AND due_at<=now()) OR (status='sending' AND lease_until<now())
                   ORDER BY due_at,id LIMIT 1 FOR UPDATE""",
            ).fetchone()
            if not row:
                return 0
            if row["attempts"] >= 6:
                conn.execute(
                    "UPDATE business.notification_outbox SET status='dead',error='投递重试次数已达上限' WHERE id=%s",
                    (row["id"],),
                )
                return 1
            epoch = row["execution_epoch"] + 1
            conn.execute(
                """UPDATE business.notification_outbox SET status='sending',attempts=attempts+1,
                   execution_epoch=%s,lease_until=now()+interval '30 seconds' WHERE id=%s""", (epoch, row["id"]),
            )
            channel = conn.execute(
                "SELECT * FROM business.notification_channels WHERE id=%s", (row["channel_id"],),
            ).fetchone()
            event = conn.execute("SELECT event FROM business.alerts WHERE id=%s", (row["alert_id"],)).fetchone()["event"]
        status, error = "sent", None
        try:
            author = replace(context, user_id=channel["user_id"])
            with authorize(self.identity, author, channel["auth_version"]), documents.transaction(write=False) as unit:
                current = unit.connection.execute(
                    "SELECT * FROM business.notification_channels WHERE id=%s FOR SHARE", (channel["id"],),
                ).fetchone()
                attempt = unit.connection.execute(
                    "SELECT * FROM business.notification_outbox WHERE id=%s FOR SHARE", (row["id"],),
                ).fetchone()
                if (not current["enabled"] or current["version"] != row["channel_version"]
                        or current["policy_version"] != context.policy_version
                        or current["routing_epoch"] != context.routing_epoch):
                    raise AccessError(403, "通知渠道授权已变更")
                if (attempt["status"] != "sending" or attempt["execution_epoch"] != epoch
                        or attempt["lease_until"] <= datetime.now(UTC)):
                    return 0
                self.transport.send(
                    channel, {**event, "message": event.get("content") or event["message"]}, row["id"],
                )
        except AccessError as exc:
            status, error = "cancelled", exc.code
        except DeliveryError as exc:
            status = "queued" if exc.retryable and row["attempts"] + 1 < 6 else "dead"
            error = exc.code
        except Exception:
            status, error = "dead", "通知投递失败"
        with authorize(self.identity, context, account["auth_version"]), documents.transaction() as unit:
            unit.connection.execute(
                """UPDATE business.notification_outbox SET status=%s,error=%s,lease_until=NULL,
                   sent_at=%s,due_at=%s WHERE id=%s AND status='sending' AND execution_epoch=%s
                   AND lease_until>now()""",
                (status, error, datetime.now(UTC) if status == "sent" else None,
                 datetime.now(UTC) + timedelta(seconds=min(300, 5 * 2 ** row["attempts"])), row["id"], epoch),
            )
        return 1


@router.get("/channels")
def channels(request: Request):
    documents = private_storage(request)
    return {"channels": Notifications(documents.db).channels(documents)}


@router.post("/channels")
def save_channel(request: Request, body: ChannelRequest):
    documents = private_storage(request)
    return {"channels": Notifications(documents.db).save(documents, request.state.account, body.model_dump())}


@router.get("/preferences")
def preferences(request: Request):
    return private_storage(request).get("notification_preferences", "default", PreferencesRequest().model_dump())


@router.put("/preferences")
def save_preferences(request: Request, body: PreferencesRequest):
    documents = private_storage(request)
    with documents.transaction() as unit:
        selected = set(body.default_channels + body.review_channels)
        available = {row["id"] for row in Notifications(documents.db).channels(unit)}
        if not selected.issubset(available):
            raise AccessError(400, "通知渠道不存在")
        return unit.put("notification_preferences", "default", body.model_dump())


@router.get("/deliveries")
def deliveries(request: Request):
    documents = private_storage(request)
    with documents._transaction() as conn:
        return {"deliveries": conn.execute(
            """SELECT id,channel_id,alert_id,status,attempts,error,created_at,sent_at
               FROM business.notification_outbox ORDER BY created_at DESC LIMIT 100""",
        ).fetchall()}

@router.get("/review-schedule")
def review_schedule(request: Request):
    value = private_storage(request).get("review_schedule", "default", ReviewScheduleRequest().model_dump())
    return {key: value[key] for key in ("enabled", "hour", "minute")}


@router.post("/review-schedule")
def save_review_schedule(request: Request, body: ReviewScheduleRequest):
    documents = private_storage(request)
    context = documents.context
    account = request.state.account
    with authorize(Notifications(documents.db).identity, context, account["auth_version"]):
        documents.put("review_schedule", "default", {
            **body.model_dump(), "user_id": account["id"], "auth_version": account["auth_version"],
            "policy_version": context.policy_version, "routing_epoch": context.routing_epoch,
        })
    return body.model_dump()


@router.post("/deliveries/{delivery_id}/retry")
def retry(request: Request, delivery_id: str):
    documents = private_storage(request)
    with documents.transaction() as unit:
        row = unit.connection.execute(
            """UPDATE business.notification_outbox o SET status='queued',attempts=0,error=NULL,due_at=now()
               FROM business.notification_channels c WHERE o.id=%s AND o.channel_id=c.id AND c.enabled
               AND o.channel_version=c.version AND o.status='dead' RETURNING o.id""", (delivery_id,),
        ).fetchone()
        if not row:
            raise AccessError(409, "该通知不可重试")
    return {"ok": True}
