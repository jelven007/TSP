"""Registration SMTP defaults and account-specific notification recipients."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, secrets_store
from app.api import settings as settings_api
from app.services import auth, auth_verification, email_adapter, preferences


@pytest.fixture(autouse=True)
def isolated_email_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config, "_IS_FROZEN", False)
    for field, value in {
        "auth_smtp_host": "smtp.example.com",
        "auth_smtp_port": 587,
        "auth_smtp_security": "starttls",
        "auth_smtp_username": "registration@example.com",
        "auth_smtp_password": "registration-secret",
        "auth_smtp_from_address": "",
    }.items():
        monkeypatch.setattr(config.settings, field, value)
    auth._sessions.clear()
    auth._configured_cache = None
    preferences._invalidate_cache()
    secrets_store._cache = None
    secrets_store._cache_sig = None
    yield tmp_path
    auth._sessions.clear()
    auth._configured_cache = None
    preferences._invalidate_cache()
    secrets_store._cache = None
    secrets_store._cache_sig = None


def client_for(email: str) -> TestClient:
    session, _ = auth.register_user(email, "password-123")
    app = FastAPI()
    app.include_router(settings_api.router)
    client = TestClient(app)
    client.cookies.set("tf_session", session)
    return client


def test_notification_defaults_reuse_registration_transport_without_copying_secret():
    auth.register_user("User@Example.com", "password-123")

    smtp, password = email_adapter.get_notification_smtp()
    registration, registration_password = auth_verification._registration_smtp("user@example.com")

    assert smtp == registration
    assert smtp["from_address"] == "registration@example.com"
    assert smtp["to_addresses"] == ["user@example.com"]
    assert password == registration_password == "registration-secret"
    assert preferences.get_webhook_default_channels() == ["email"]
    assert preferences.load() == {}
    assert secrets_store.load() == {}


def test_each_account_sees_its_own_default_recipient(monkeypatch):
    monkeypatch.setattr(settings_api, "_realtime_allowed", lambda: False)
    monkeypatch.setattr(settings_api, "_minute_history_days", lambda: None)
    first = client_for("first@example.com")
    second = client_for("second@example.com")

    for client, recipient in ((first, "first@example.com"), (second, "second@example.com")):
        response = client.get("/api/settings/preferences")
        assert response.status_code == 200
        result = response.json()
        assert result["email_smtp_config"]["to_addresses"] == [recipient]
        assert result["email_smtp_config"]["port"] == 587
        assert result["email_smtp_password_set"] is True
        assert result["webhook_default_channels"] == ["email"]
        assert "registration-secret" not in response.text
    assert preferences.load() == {}
    smtp, _ = email_adapter.get_notification_smtp()
    assert smtp["to_addresses"] == ["first@example.com", "second@example.com"]


def test_test_email_uses_current_account_and_inherited_password(monkeypatch):
    client = client_for("first@example.com")
    auth.register_user("second@example.com", "password-123")
    sent = []
    monkeypatch.setattr(
        email_adapter, "send_email",
        lambda smtp, password, subject, body, **kwargs:
            sent.append((smtp, password, kwargs)) or True,
    )

    response = client.post("/api/settings/preferences/webhook-test", json={"channel": "email"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(sent) == 1
    assert sent[0][0]["to_addresses"] == ["first@example.com"]
    assert sent[0][1] == "registration-secret"
    assert sent[0][2] == {"max_attempts": 1}


def test_saving_recipients_preserves_registration_transport_and_secret():
    client = client_for("user@example.com")
    response = client.put("/api/settings/preferences/email-smtp", json={"to_addresses": []})

    assert response.status_code == 200
    assert response.json()["email_smtp_config"]["to_addresses"] == ["user@example.com"]
    assert response.json()["email_smtp_password_set"] is True
    assert email_adapter.get_notification_smtp()[1] == "registration-secret"
    assert preferences.load() == {"email_smtp_config": {"to_addresses": ["user@example.com"]}}
    assert secrets_store.load() == {}
    assert "registration-secret" not in response.text
    assert "registration-secret" not in preferences._path().read_text()


@pytest.mark.parametrize(
    ("field", "value"),
    [("host", "different.example.com"), ("port", 465), ("security", "ssl"),
     ("username", "different@example.com"), ("password", "replacement-secret"),
     ("from_address", "different@example.com")],
)
def test_notification_api_rejects_all_transport_changes(field, value):
    client = client_for("user@example.com")
    response = client.put(
        "/api/settings/preferences/email-smtp",
        json={"to_addresses": ["chosen@example.com"], field: value},
    )

    assert response.status_code == 422
    assert preferences.load() == {}
    assert secrets_store.load() == {}


def test_saving_custom_recipients_does_not_change_registration_delivery():
    client = client_for("user@example.com")
    response = client.put(
        "/api/settings/preferences/email-smtp",
        json={"to_addresses": [" chosen@example.com ", "chosen@example.com"]},
    )

    assert response.status_code == 200
    assert response.json()["email_smtp_config"]["to_addresses"] == ["chosen@example.com"]
    registration, password = auth_verification._registration_smtp("new@example.com")
    assert registration["to_addresses"] == ["new@example.com"]
    assert password == "registration-secret"
    assert secrets_store.load() == {}


def test_registration_transport_overrides_old_custom_smtp_but_keeps_recipients():
    auth.register_user("user@example.com", "password-123")
    smtp = {
        "host": "custom.example.com", "port": 465, "security": "ssl",
        "username": "custom@example.com", "from_address": "custom@example.com",
        "to_addresses": ["chosen@example.com"],
    }
    preferences.set_email_smtp_config(smtp)
    secrets_store.set_email_smtp_password("custom-secret")
    preferences.set_webhook_default_channels([])

    effective, password = email_adapter.get_notification_smtp("user@example.com")
    assert effective["host"] == "smtp.example.com"
    assert effective["username"] == "registration@example.com"
    assert effective["to_addresses"] == ["chosen@example.com"]
    assert password == "registration-secret"
    assert preferences.get_webhook_default_channels() == []
    assert auth_verification._registration_smtp("new@example.com")[1] == "registration-secret"


def test_legacy_empty_smtp_group_uses_system_transport():
    preferences.set_email_smtp_config({"host": ""})
    auth.register_user("user@example.com", "password-123")

    smtp, password = email_adapter.get_notification_smtp()

    assert smtp["host"] == "smtp.example.com"
    assert smtp["to_addresses"] == ["user@example.com"]
    assert password == "registration-secret"
    assert preferences.get_webhook_default_channels() == ["email"]


def test_recipient_save_preserves_legacy_system_transport_when_env_is_unset(monkeypatch):
    monkeypatch.setattr(config.settings, "auth_smtp_host", "")
    client = client_for("user@example.com")
    preferences.set_email_smtp_config({
        "host": "legacy.example.com", "port": 465, "security": "ssl",
        "username": "legacy@example.com", "from_address": "legacy@example.com",
        "to_addresses": ["old@example.com"],
    })
    secrets_store.set_email_smtp_password("legacy-secret")
    previous_secrets = secrets_store._path().read_bytes()

    response = client.put(
        "/api/settings/preferences/email-smtp", json={"to_addresses": ["new@example.com"]},
    )

    assert response.status_code == 200
    assert response.json()["email_smtp_config"]["host"] == "legacy.example.com"
    assert preferences.get_email_smtp_config()["to_addresses"] == ["new@example.com"]
    assert secrets_store._path().read_bytes() == previous_secrets


def test_legacy_channel_opt_out_overrides_new_email_default():
    auth.register_user("user@example.com", "password-123")
    preferences.save({"webhook_enabled_default": False})

    assert preferences.get_webhook_default_channels() == []


@pytest.mark.parametrize("mode", ["no_users", "no_transport", "no_password", "desktop"])
def test_email_default_requires_a_usable_transport_and_registered_recipient(mode, monkeypatch):
    if mode != "no_users":
        auth.register_user("user@example.com", "password-123")
    if mode == "no_transport":
        monkeypatch.setattr(config.settings, "auth_smtp_host", "")
    elif mode == "no_password":
        monkeypatch.setattr(config.settings, "auth_smtp_password", "")
    elif mode == "desktop":
        monkeypatch.setattr(config, "_IS_FROZEN", True)

    assert preferences.get_webhook_default_channels() == []


def test_request_without_current_account_does_not_inherit_other_recipients(monkeypatch):
    monkeypatch.setattr(settings_api, "_realtime_allowed", lambda: False)
    monkeypatch.setattr(settings_api, "_minute_history_days", lambda: None)
    client = client_for("user@example.com")
    client.cookies.clear()

    result = client.get("/api/settings/preferences").json()

    assert result["email_smtp_config"]["to_addresses"] == []


def test_registration_still_falls_back_to_saved_notification_smtp(monkeypatch):
    monkeypatch.setattr(config.settings, "auth_smtp_host", "")
    preferences.set_email_smtp_config({
        "host": "custom.example.com", "username": "custom@example.com",
        "from_address": "custom@example.com", "to_addresses": ["alerts@example.com"],
    })
    secrets_store.set_email_smtp_password("custom-secret")

    smtp, password = auth_verification._registration_smtp("new@example.com")

    assert smtp["host"] == "custom.example.com"
    assert smtp["to_addresses"] == ["new@example.com"]
    assert password == "custom-secret"


def test_monitor_dispatch_uses_registration_defaults(monkeypatch):
    from app.services import quote_service

    auth.register_user("user@example.com", "password-123")
    calls = []

    class CaptureExecutor:
        def submit(self, fn, *args):
            calls.append((fn, args))

    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", CaptureExecutor())
    engine = type("Engine", (), {"rules": {"price": {"webhook_channels": ["email"]}}})()

    quote_service.QuoteService._maybe_send_webhook(
        object.__new__(quote_service.QuoteService),
        [{"rule_id": "price", "source": "price", "symbol": "600000.SH", "message": "价格触发"}],
        engine,
    )

    assert len(calls) == 1
    fn, args = calls[0]
    assert fn is email_adapter.send_email
    assert args[0]["to_addresses"] == ["user@example.com"]
    assert args[1] == "registration-secret"
    assert preferences.load() == {}


def test_review_dispatch_uses_same_registration_defaults(monkeypatch):
    from app.jobs import daily_pipeline

    auth.register_user("user@example.com", "password-123")
    preferences.set_review_push_channels(["email"])
    calls = []
    monkeypatch.setattr(
        email_adapter, "send_email",
        lambda *args: calls.append(args) or True,
    )

    daily_pipeline._maybe_push_review("复盘正文", {"as_of": "2026-10-13"})

    assert len(calls) == 1
    assert calls[0][0]["to_addresses"] == ["user@example.com"]
    assert calls[0][1] == "registration-secret"
    assert calls[0][2] == "每日复盘"
