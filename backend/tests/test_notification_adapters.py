from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import ClassVar

from app.services import email_adapter, webhook_adapter


def test_custom_webhook_posts_stable_signed_json(monkeypatch):
    captured = {}

    class Response:
        status_code = 204
        text = ""

    def fake_post(url, *, content, headers, timeout):
        captured.update(url=url, content=content, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(webhook_adapter.time, "time", lambda: 1_700_000_000)

    assert webhook_adapter.send_custom(
        "https://example.com/tickflow",
        "价格预警",
        "600000.SH 触发",
        "monitor_alert",
        {"symbol": "600000.SH"},
        "shared-secret",
    )
    payload = json.loads(captured["content"])
    assert payload == {
        "event": "monitor_alert",
        "timestamp": 1_700_000_000,
        "title": "价格预警",
        "body": "600000.SH 触发",
        "data": {"symbol": "600000.SH"},
    }
    expected = hmac.new(b"shared-secret", captured["content"], hashlib.sha256).hexdigest()
    assert captured["headers"]["X-TickFlow-Signature"] == f"sha256={expected}"
    assert captured["headers"]["X-TickFlow-Timestamp"] == "1700000000"


def test_custom_webhook_rejects_non_http_urls(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
    assert not webhook_adapter.send_custom("file:///tmp/hook", "title", "body", "test")


def test_email_adapter_uses_starttls_login_and_multiple_recipients(monkeypatch):
    instances = []

    class FakeSmtp:
        esmtp_features: ClassVar[dict[str, str]] = {}

        def __init__(self, host, port, timeout):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.calls = []
            instances.append(self)

        def ehlo(self):
            self.calls.append(("ehlo",))

        def ehlo_or_helo_if_needed(self):
            pass

        def starttls(self):
            self.calls.append(("starttls",))

        def login(self, username, password):
            self.calls.append(("login", username, password))

        def send_message(self, message):
            self.calls.append(("send", message))

        def quit(self):
            self.calls.append(("quit",))

        def close(self):
            self.calls.append(("close",))

    monkeypatch.setattr(email_adapter.smtplib, "SMTP", FakeSmtp)
    config = {
        "host": "smtp.example.com",
        "port": 587,
        "security": "starttls",
        "username": "bot@example.com",
        "from_address": "bot@example.com",
        "to_addresses": ["one@example.com", "two@example.com"],
    }

    assert email_adapter.send_email(config, "smtp-password", "监控告警", "正文")
    smtp = instances[0]
    assert smtp.calls[:4] == [
        ("ehlo",),
        ("starttls",),
        ("ehlo",),
        ("login", "bot@example.com", "smtp-password"),
    ]
    message = next(call[1] for call in smtp.calls if call[0] == "send")
    assert message["To"] == "one@example.com, two@example.com"
    assert message["Subject"] == "监控告警"
    assert smtp.calls[-1] == ("quit",)


def test_email_adapter_prefers_auth_login_for_netease_mailboxes(monkeypatch):
    # 126/163 宣称支持 AUTH PLAIN, 但用客户端授权码会返回 535, 必须优先走 AUTH LOGIN。
    instances = []

    class FakeSmtp:
        esmtp_features: ClassVar[dict[str, str]] = {"auth": " LOGIN PLAIN XOAUTH2"}

        def __init__(self, host, port, timeout):
            self.calls = []
            self.user = ""
            self.password = ""
            instances.append(self)

        def ehlo_or_helo_if_needed(self):
            pass

        def auth_login(self, challenge):
            self.calls.append(("auth_login", challenge))
            return "QTsystem@126.com"

        def auth(self, mechanism, authobject, *, initial_response_ok=True):
            self.calls.append(("auth", mechanism, initial_response_ok))
            return 235, b"Authentication successful"

        def login(self, username, password):
            raise AssertionError("must prefer AUTH LOGIN over AUTH PLAIN")

        def send_message(self, message):
            self.calls.append(("send", message))

        def quit(self):
            self.calls.append(("quit",))

        def close(self):
            self.calls.append(("close",))

    monkeypatch.setattr(email_adapter.smtplib, "SMTP_SSL", FakeSmtp)
    config = {
        "host": "smtp.126.com",
        "port": 465,
        "security": "ssl",
        "username": "QTsystem@126.com",
        "from_address": "QTsystem@126.com",
        "to_addresses": ["one@example.com"],
    }

    assert email_adapter.send_email(config, "authorization-code", "验证码", "正文")
    smtp = instances[0]
    assert smtp.calls[0] == ("auth", "LOGIN", False)
    assert smtp.user == "QTsystem@126.com"
    assert smtp.password == "authorization-code"
    assert smtp.calls[1][0] == "send"
    assert smtp.calls[-1] == ("quit",)


def test_full_notification_transport_uses_auth_login_for_configured_mailbox(monkeypatch):
    from app import config
    from app.cell.config import CellSettings
    from app.cell.notifications import NotificationTransport
    from app.multitenancy.notifications import FullNotificationTransport

    calls = []

    class FakeSmtp:
        esmtp_features: ClassVar[dict[str, str]] = {"auth": "PLAIN LOGIN"}

        def __init__(self, host, port, **kwargs):
            calls.append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def ehlo_or_helo_if_needed(self):
            pass

        def auth_login(self, _challenge):
            return self.user

        def auth(self, mechanism, _callback, *, initial_response_ok=True):
            calls.append((mechanism, initial_response_ok, self.user, bool(self.password)))
            return 235, b"accepted"

        def login(self, *_):
            raise AssertionError("126 client authorization must prefer AUTH LOGIN")

        def send_message(self, message):
            calls.append(message["To"])

    for field, value in {
        "auth_smtp_host": "smtp.example.com", "auth_smtp_port": 465,
        "auth_smtp_security": "ssl", "auth_smtp_username": "sender@example.com",
        "auth_smtp_from_address": "sender@example.com",
        "auth_smtp_password": "test-authorization-code",
    }.items():
        monkeypatch.setattr(config.settings, field, value)
    monkeypatch.setattr(email_adapter.smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr("app.services.preferences.get_email_smtp_config", lambda: {"host": ""})
    transport = FullNotificationTransport(CellSettings(id="test", control_dsn="", data_dsn=""))
    effective = transport.configuration()
    NotificationTransport(effective).send_direct(
        {"kind": "email", "destination": "user@example.com"}, {"message": "test"}, "test-id",
    )
    assert calls == [
        ("smtp.example.com", 465), ("LOGIN", False, "sender@example.com", True), "user@example.com",
    ]


def _unreachable(*_args, **_kwargs):
    raise ConnectionError("unreachable")


def test_feishu_single_attempt_returns_without_backoff_and_logs_the_real_count(monkeypatch, caplog):
    # 设置页「发送测试消息」传 max_attempts=1: 失败即返回, 不等退避, 日志计数如实。
    sleeps: list[float] = []
    monkeypatch.setattr(webhook_adapter.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("httpx.post", _unreachable)
    with caplog.at_level(logging.WARNING, logger=webhook_adapter.__name__):
        ok = webhook_adapter.send_feishu(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc", "标题", "正文", max_attempts=1
        )
    assert ok is False
    assert sleeps == []
    assert "已重试 1 次" in caplog.text


def test_feishu_production_retries_keep_backoff(monkeypatch, caplog):
    # 生产路径 (默认 3 次) 的退避与日志不变: 1s、2s 两次退避, 日志写 3 次。
    sleeps: list[float] = []
    calls = {"n": 0}

    def unreachable(*_args, **_kwargs):
        calls["n"] += 1
        raise ConnectionError("unreachable")

    monkeypatch.setattr(webhook_adapter.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("httpx.post", unreachable)
    with caplog.at_level(logging.WARNING, logger=webhook_adapter.__name__):
        ok = webhook_adapter.send_feishu(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc", "标题", "正文"
        )
    assert ok is False
    assert calls["n"] == 3
    assert sleeps == [1, 2]
    assert "已重试 3 次" in caplog.text
