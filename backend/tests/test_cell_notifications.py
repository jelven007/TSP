"""Notification failures/restarts/isolation against SQL, with local HTTP reception only."""

# ruff: noqa: F811
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

pytest.importorskip("psycopg")
from test_cell_isolation import cluster, env, headers, path  # noqa: F401

from app.cell.alerts import append_alert
from app.cell.context import AccessError
from app.cell.identity import Identity
from app.cell.notifications import DeliveryError, Notifications, NotificationTransport


class StubTransport:
    def __init__(self):
        self.calls = []
        self.fail = True
    def validate_destination(self, kind, destination):
        pass
    def send(self, channel, event, delivery_id):
        self.calls.append(delivery_id)
        if self.fail:
            raise DeliveryError("notification_connection_failed", True)


def configure(env, transport, user="owner"):
    db = env["clients"]["a"].app.state.databases
    notifications = Notifications(db, transport)
    with Identity(db).authorized(env["tokens"][user], env["tenants"]["alpha"], 1, "test", "alerts:write") as (ctx, _):
        channel = notifications.save(ctx, {
            "name": "通知", "kind": "webhook", "destination": "https://notify.test/events", "enabled": True,
        })
        with db.business_transaction(ctx, write=True, permission="alerts:write") as conn:
            append_alert(conn, ctx.tenant_id, "one", {"message": "单次提醒", "severity": "info"})
            append_alert(conn, ctx.tenant_id, "one", {"message": "单次提醒", "severity": "info"})
        return db, notifications, channel


def test_outbox_retry_restart_dedupe_and_isolation(env):
    transport = StubTransport()
    db, notifications, _channel = configure(env, transport)
    tenant = env["tenants"]["alpha"]
    notifications.process(tenant)
    row = env["data"]["a"].execute("SELECT * FROM business.notification_outbox WHERE tenant_id=%s", (tenant,)).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 1 and row["error"] == "notification_connection_failed"
    assert len(transport.calls) == 1
    env["data"]["a"].execute("UPDATE business.notification_outbox SET due_at=now() WHERE tenant_id=%s", (tenant,))
    transport.fail = False
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: Notifications(db, transport).process(tenant), range(2)))
    row = env["data"]["a"].execute("SELECT * FROM business.notification_outbox WHERE tenant_id=%s", (tenant,)).fetchone()
    assert row["status"] == "sent" and row["attempts"] == 2 and row["sent_at"]
    assert transport.calls == [row["id"], row["id"]]
    assert env["clients"]["a"].get(path(env, "beta", "/notification-deliveries"), headers=headers(env, "other")).json()["deliveries"] == []
    assert env["clients"]["a"].get(path(env, "beta", "/notification-channels"), headers=headers(env, "other")).json()["channels"] == []


def test_notification_permission_policy_and_channel_pause(env):
    transport = StubTransport()
    db, notifications, _channel = configure(env, transport, "admin")
    tenant = env["tenants"]["alpha"]
    env["clients"]["a"].put(path(env, suffix="/members/" + env["users"]["admin"]),
                            headers=headers(env), json={"role": "member"})
    notifications.process(tenant)
    assert transport.calls == []
    assert env["data"]["a"].execute(
        "SELECT status FROM business.notification_outbox WHERE tenant_id=%s", (tenant,)
    ).fetchone()["status"] == "cancelled"
    with Identity(db).authorized(env["tokens"]["member"], tenant, 1, "test", "alerts:write") as (ctx, _), pytest.raises(
        AccessError, match="notification_admin_required"
    ):
        notifications.save(ctx, {"name": "bad", "kind": "email", "destination": "a@test", "enabled": True})


def test_delivery_dead_letter_and_manual_retry(env):
    transport = StubTransport()
    db, notifications, _ = configure(env, transport)
    tenant = env["tenants"]["alpha"]
    env["data"]["a"].execute("UPDATE business.notification_outbox SET attempts=5 WHERE tenant_id=%s", (tenant,))
    notifications.process(tenant)
    row = env["data"]["a"].execute("SELECT * FROM business.notification_outbox WHERE tenant_id=%s", (tenant,)).fetchone()
    assert row["status"] == "dead" and row["attempts"] == 6
    with Identity(db).authorized(env["tokens"]["owner"], tenant, 1, "test", "alerts:write") as (ctx, _):
        notifications.retry(ctx, row["id"])
    transport.fail = False
    notifications.process(tenant)
    assert env["data"]["a"].execute(
        "SELECT status FROM business.notification_outbox WHERE tenant_id=%s", (tenant,)
    ).fetchone()["status"] == "sent"


def test_real_local_http_delivery_contract_and_redirect_rejection(monkeypatch, env):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.headers["Idempotency-Key"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = NotificationTransport(env["settings"][0].model_copy(update={"webhook_origins": ["https://notify.test"]}))
    original_stream = httpx.Client.stream
    def local_stream(client, method, url, **kwargs):
        assert url == "https://notify.test/events"
        return original_stream(client, method, f"http://127.0.0.1:{server.server_port}/", **kwargs)
    monkeypatch.setattr(httpx.Client, "stream", local_stream)
    try:
        transport.send_direct({"kind": "webhook", "destination": "https://notify.test/events"}, {"message": "本地测试"}, "id-1")
        assert received == [("id-1", {"id": "id-1", "event": {"message": "本地测试"}})]
        with pytest.raises(AccessError, match="webhook_origin_not_allowed"):
            transport.validate_destination("webhook", "http://127.0.0.1/private")
        with pytest.raises(AccessError):
            transport.validate_destination("webhook", "https://notify.test@127.0.0.1/private")
        from contextlib import nullcontext
        monkeypatch.setattr(httpx.Client, "stream", lambda *args, **kwargs: nullcontext(httpx.Response(302)))
        with pytest.raises(DeliveryError) as exc:
            transport.send_direct({"kind": "webhook", "destination": "https://notify.test/events"}, {}, "id-2")
        assert not exc.value.retryable
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_smtp_tls_contract_and_expired_delivery_restart(monkeypatch, env):
    messages = []
    class SMTP:
        def __init__(self, host, port, timeout, context):
            assert host == "smtp.test" and port == 465 and timeout == 5
            assert context.verify_mode == __import__("ssl").CERT_REQUIRED
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def login(self, user, password):
            assert user == "smtp-user" and password == "local-test-password"
        def send_message(self, message):
            messages.append(message)
    from pydantic import SecretStr
    settings = env["settings"][0].model_copy(update={
        "smtp_host": "smtp.test", "smtp_from": "sender@example.test", "smtp_user": "smtp-user",
        "smtp_password": SecretStr("local-test-password"),
    })
    monkeypatch.setattr("smtplib.SMTP_SSL", SMTP)
    NotificationTransport(settings).send_direct(
        {"kind": "email", "destination": "receiver@example.test"}, {"message": "邮件测试"}, "delivery-1"
    )
    assert messages[0]["Message-ID"] == "<delivery-1@tsp-notifications>"
    assert messages[0]["To"] == "receiver@example.test"
    assert "邮件测试" in messages[0].get_content()
    transport = StubTransport()
    db, _notifications, _channel = configure(env, transport)
    tenant = env["tenants"]["alpha"]
    env["data"]["a"].execute(
        """UPDATE business.notification_outbox SET status='sending',attempts=1,execution_epoch=1,
           lease_until=now()-interval '1 second' WHERE tenant_id=%s""", (tenant,),
    )
    transport.fail = False
    Notifications(db, transport).process(tenant)
    row = env["data"]["a"].execute(
        "SELECT status,attempts,execution_epoch FROM business.notification_outbox WHERE tenant_id=%s", (tenant,)
    ).fetchone()
    assert row == {"status": "sent", "attempts": 2, "execution_epoch": 2}


def test_notification_subprocess_wall_deadline_and_env(monkeypatch, env):
    import subprocess
    settings = env["settings"][0].model_copy(update={"webhook_origins": ["https://notify.test"]})
    observed = []
    def timeout(command, **kwargs):
        observed.append(kwargs)
        assert kwargs["timeout"] == 8
        assert not any("DSN" in name or "TOKEN" in name or "PASSWORD" in name for name in kwargs["env"])
        raise subprocess.TimeoutExpired(command, 8)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(DeliveryError) as exc:
        NotificationTransport(settings).send({"kind": "webhook", "destination": "https://notify.test/events"}, {}, "wall")
    assert exc.value.retryable and exc.value.code == "notification_timeout" and len(observed) == 1


def test_real_delivery_subprocess_rejects_unapproved_origin(env):
    # Starts the production isolated process; rejection occurs before any network request.
    transport = NotificationTransport(env["settings"][0])
    with pytest.raises(DeliveryError) as exc:
        transport.send({"kind": "webhook", "destination": "https://unapproved.test/events"}, {}, "isolated")
    assert exc.value.code == "webhook_origin_not_allowed" and not exc.value.retryable
