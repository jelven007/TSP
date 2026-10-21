"""Windows single-account runtime: login, personal data and upgrade safety."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.api import auth as auth_api
from app.main import auth_middleware
from app.services import auth


@pytest.fixture
def local_admin(monkeypatch, tmp_path):
    monkeypatch.setattr(config.settings, "local_admin_mode", True)
    monkeypatch.setattr(config.settings, "service_role", "standalone")
    monkeypatch.setattr(config.settings, "multitenancy_enabled", False)
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()
    yield tmp_path
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()


def test_windows_defaults_to_single_admin(monkeypatch):
    monkeypatch.delenv("LOCAL_ADMIN_MODE", raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    settings = config.Settings(
        _env_file=None,
        local_admin_mode=False,
        multitenancy_enabled=True,
    )
    assert settings.local_admin_mode is True
    assert settings.multitenancy_enabled is False
    assert settings.service_role == "standalone"
    with pytest.raises(ValueError, match="SERVICE_ROLE=standalone"):
        config.Settings(_env_file=None, service_role="platform")


def test_other_platform_retains_multitenancy(monkeypatch):
    monkeypatch.delenv("LOCAL_ADMIN_MODE", raising=False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    settings = config.Settings(_env_file=None, service_role="api", multitenancy_enabled=True)
    assert settings.local_admin_mode is False
    assert settings.multitenancy_enabled is True
    assert settings.service_role == "api"


def test_bootstrap_preserves_legacy_identity_and_changed_password(local_admin):
    legacy = local_admin / "user_data" / "auth.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"legacy": "preserve"}', encoding="utf-8")
    assert auth.bootstrap_local_admin() is True
    token, user = auth.authenticate_user("admin", "Admin123")
    assert user["username"] == "admin"
    assert user["role"] == "admin"
    assert user["email"] is None
    assert user["default_tenant_id"] == "personal"
    assert user["workspace"] == {"id": "personal", "name": "个人空间"}
    assert "password_hash" not in user
    assert auth.registered_emails() == []
    assert auth.authenticate_user("other", "Admin123") is None
    assert auth.authenticate_user("admin@example.com", "Admin123") is None
    assert auth.change_password(token, "Admin123", "changed-password") is True
    assert auth.bootstrap_local_admin() is False
    assert auth.authenticate_user("admin", "Admin123") is None
    token, after = auth.authenticate_user("admin", "changed-password")
    assert after == user
    auth._sessions.clear()
    auth._restore_sessions()
    assert auth.current_user(token) == user
    assert legacy.read_text(encoding="utf-8") == '{"legacy": "preserve"}'
    saved = (legacy.parent / "local_admin.json").read_text(encoding="utf-8")
    assert "Admin123" not in saved
    assert "changed-password" not in saved
    assert len(json.loads(saved)["users"]) == 1


@pytest.mark.parametrize("contents", ["{", "{}", "[]", '{"users": {}}'])
def test_damaged_admin_file_never_resets_password(local_admin, contents):
    path = local_admin / "user_data" / "local_admin.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"local_admin\.json"):
        auth.bootstrap_local_admin()
    assert path.read_text(encoding="utf-8") == contents


def test_local_admin_api_even_in_frozen_build(local_admin, monkeypatch):
    monkeypatch.setattr(config, "_IS_FROZEN", True)
    auth.bootstrap_local_admin()
    app = FastAPI()
    app.middleware("http")(auth_middleware)
    app.include_router(auth_api.router)

    @app.get("/api/personal")
    def personal():
        return {"saved": True}

    client = TestClient(app)
    status = client.get("/api/auth/status").json()
    assert status["local_admin_mode"] is True
    assert status["multitenancy_enabled"] is False
    assert status["auth_required"] is True
    assert status["registration_enabled"] is False
    assert status["email_verification_required"] is False
    assert status["authenticated"] is False
    assert client.get("/api/personal").status_code == 401
    assert client.post("/api/auth/login", json={"password": "Admin123"}).status_code == 400
    assert client.post("/api/auth/login", json={
        "email": "other", "password": "Admin123",
    }).status_code == 401
    response = client.post("/api/auth/login", json={"email": "admin", "password": "Admin123"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/api/personal").json() == {"saved": True}
    assert client.get("/api/auth/status").json()["user"]["workspace"]["id"] == "personal"
    for path in ("register", "register/code", "recovery/code", "recovery/reset", "setup", "migrate"):
        assert client.post(f"/api/auth/{path}", json={}).status_code == 403
    assert client.get("/api/account/tenants").status_code == 403
    assert client.post("/api/auth/change-password", json={
        "old_password": "Admin123", "new_password": "changed-password",
    }).status_code == 200
    assert client.get("/api/personal").status_code == 401
    assert client.post("/api/auth/login", json={
        "email": "admin", "password": "changed-password",
    }).status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/personal").status_code == 401


def test_admin_personal_watchlist_survives_logout_and_restart(local_admin):
    from app.api import watchlist

    auth.bootstrap_local_admin()
    app = FastAPI()
    app.middleware("http")(auth_middleware)
    app.include_router(auth_api.router)
    app.include_router(watchlist.router)
    client = TestClient(app)
    login = {"email": "admin", "password": "Admin123"}
    assert client.post("/api/watchlist", json={"symbol": "600000.SH"}).status_code == 401
    assert client.post("/api/auth/login", json=login).status_code == 200
    added = client.post("/api/watchlist", json={"symbol": "600000.SH", "note": "个人研究"})
    assert added.status_code == 200
    assert added.json()["symbols"][0]["symbol"] == "600000.SH"
    assert (local_admin / "user_data" / "watchlist.parquet").exists()
    client.post("/api/auth/logout")
    assert client.get("/api/watchlist").status_code == 401
    auth._sessions.clear()
    auth._configured_cache = None
    auth.bootstrap_local_admin()
    assert client.post("/api/auth/login", json=login).status_code == 200
    assert client.get("/api/watchlist").json()["symbols"][0]["note"] == "个人研究"
