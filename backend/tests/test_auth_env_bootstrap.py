from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from app import config as app_config
from app import main
from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_auth_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[ModuleType, Path, Path]]:
    monkeypatch.setattr(app_config.sys, "platform", "linux")
    monkeypatch.setattr(app_config.settings, "local_admin_mode", False)
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    from app.services import auth

    auth_path = tmp_path / "user_data" / "auth.json"
    env_path = tmp_path / ".env"
    monkeypatch.setattr(app_config, "_ENV_FILE", env_path)
    monkeypatch.setattr(app_config.settings, "auth_username", "")
    monkeypatch.setattr(app_config.settings, "auth_email", "")
    monkeypatch.setattr(app_config.settings, "auth_password", "")
    auth._sessions.clear()
    auth._configured_cache = None
    yield auth, auth_path, env_path
    auth._sessions.clear()
    auth._configured_cache = None


def test_bootstrap_recovers_compose_interpolated_password_from_raw_env(
    isolated_auth_store: tuple[ModuleType, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    auth, auth_path, env_path = isolated_auth_store
    password = "pw${special}-secret"
    env_path.write_text(
        f"AUTH_PASSWORD={password}\nDATA_DIR={tmp_path}\n",
        encoding="utf-8",
    )
    configured = Settings(
        _env_file=env_path, data_dir=tmp_path, local_admin_mode=False, multitenancy_enabled=False,
    )
    configured.auth_password = "pw-secret"  # 模拟 Compose 将未定义的 ${special} 插值为空串
    monkeypatch.setattr(app_config, "settings", configured)

    assert auth.bootstrap_from_env() is True
    assert auth_path.exists()
    assert password not in auth_path.read_text(encoding="utf-8")
    assert auth.verify_and_create_session(password) is not None
    assert auth.verify_and_create_session("pw-secret") is None


def test_bootstrap_from_env_does_not_override_existing_password(
    isolated_auth_store: tuple[ModuleType, Path, Path],
) -> None:
    auth, auth_path, env_path = isolated_auth_store
    auth.set_password("web-managed-secret")
    before = auth_path.read_bytes()
    env_path.write_text("AUTH_PASSWORD=replacement-secret\n", encoding="utf-8")
    app_config.settings.auth_password = "replacement-secret"

    assert auth.bootstrap_from_env() is False
    assert auth_path.read_bytes() == before
    assert auth.verify_and_create_session("web-managed-secret") is not None
    assert auth.verify_and_create_session("replacement-secret") is None


def test_admin_preseed_keeps_macos_email_mode_and_existing_accounts(
    isolated_auth_store, monkeypatch,
) -> None:
    import json

    from app.api import auth as auth_api
    from app.services import auth_verification
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    auth, auth_path, env_path = isolated_auth_store
    monkeypatch.setattr(app_config.sys, "platform", "darwin")
    auth.set_registration_secret("registration-secret")
    existing_token, existing_user = auth.register_user("owner@example.com", "owner-password")
    existing_record = json.loads(auth_path.read_text())["users"][existing_user["id"]]
    env_path.write_text("AUTH_USERNAME=admin\nAUTH_PASSWORD=Admin123\n", encoding="utf-8")

    assert auth.bootstrap_from_env() is True
    assert app_config.settings.local_admin_mode is False
    assert auth.current_user(existing_token) == existing_user
    assert json.loads(auth_path.read_text())["users"][existing_user["id"]] == existing_record
    assert auth.verify_registration_secret("registration-secret") is True
    assert auth.registered_emails() == ["owner@example.com"]
    assert not (auth_path.parent / "local_admin.json").exists()
    assert "Admin123" not in auth_path.read_text()

    auth_api._fail_counter.clear()
    app = FastAPI()
    app.include_router(auth_api.router)
    client = TestClient(app)
    status = client.get("/api/auth/status").json()
    assert status["local_admin_mode"] is False
    assert status["registration_enabled"] is True
    assert status["email_verification_required"] is True
    response = client.post("/api/auth/login", json={"email": "admin", "password": "Admin123"})
    assert response.status_code == 200
    user = response.json()["user"]
    assert user["username"] == "admin"
    assert user["role"] == "admin"
    assert "workspace" not in user
    assert "default_tenant_id" not in user
    assert client.get("/api/auth/status").json()["authenticated"] is True
    code = auth_verification.issue_code("second@example.com")
    assert client.post("/api/auth/register", json={
        "email": "second@example.com", "password": "second-password", "code": code,
    }).status_code == 201


def test_admin_preseed_is_idempotent_and_keeps_changed_password(isolated_auth_store) -> None:
    auth, auth_path, env_path = isolated_auth_store
    env_path.write_text("AUTH_USERNAME=admin\nAUTH_PASSWORD=Admin123\n", encoding="utf-8")
    assert auth.bootstrap_from_env() is True
    result = auth.authenticate_user("admin", "Admin123")
    assert result is not None
    token, user = result
    assert auth.change_password(token, "Admin123", "changed-password") is True
    before = auth_path.read_bytes()

    assert auth.bootstrap_from_env() is False
    assert auth_path.read_bytes() == before
    assert auth.authenticate_user("admin", "Admin123") is None
    assert auth.authenticate_user("admin", "changed-password")[1] == user


@pytest.mark.parametrize("username", ["invalid@name", "../admin"])
def test_admin_preseed_rejects_invalid_username_without_touching_store(
    isolated_auth_store, username,
) -> None:
    auth, auth_path, env_path = isolated_auth_store
    auth.register_user("owner@example.com", "owner-password")
    before = auth_path.read_bytes()
    env_path.write_text(f"AUTH_USERNAME={username}\nAUTH_PASSWORD=Admin123\n", encoding="utf-8")
    assert auth.bootstrap_from_env() is False
    assert auth_path.read_bytes() == before


def test_admin_preseed_cannot_bypass_legacy_password_owner(isolated_auth_store) -> None:
    auth, auth_path, env_path = isolated_auth_store
    auth.set_password("owner-password")
    before = auth_path.read_bytes()
    env_path.write_text("AUTH_USERNAME=admin\nAUTH_PASSWORD=Admin123\n", encoding="utf-8")
    assert auth.bootstrap_from_env() is False
    assert auth_path.read_bytes() == before
    assert auth.requires_legacy_migration() is True


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.asyncio
async def test_fresh_startup_preseeds_admin_without_dotenv(
    isolated_auth_store, monkeypatch, tmp_path, platform, frozen,
) -> None:
    from fastapi import FastAPI

    auth, _, env_path = isolated_auth_store
    monkeypatch.setattr(app_config.sys, "platform", platform)
    monkeypatch.setattr(app_config, "_IS_FROZEN", frozen)
    configured = Settings(_env_file=env_path, data_dir=tmp_path, multitenancy_enabled=False)
    monkeypatch.setattr(app_config, "settings", configured)
    monkeypatch.setattr(main, "settings", configured)

    class DataLayerReachedError(Exception):
        pass

    def stop_before_data():
        raise DataLayerReachedError

    monkeypatch.setattr(main, "DataStore", stop_before_data)
    with pytest.raises(DataLayerReachedError):
        async with main._application_lifespan(FastAPI()):
            pytest.fail("startup should stop at the data layer")

    result = auth.authenticate_user("admin", "Admin123")
    assert result is not None
    assert result[1]["username"] == result[1]["role"] == "admin"
    assert configured.local_admin_mode is (platform == "win32")
    assert app_config.authentication_required() is (platform == "win32" or not frozen)
    path = auth._path()
    assert path.name == ("local_admin.json" if platform == "win32" else "auth.json")
    assert "Admin123" not in path.read_text()


def test_default_bootstrap_keeps_existing_email_account_and_session(isolated_auth_store) -> None:
    auth, auth_path, _ = isolated_auth_store
    token, user = auth.register_user("owner@example.com", "owner-password")
    before = auth_path.read_bytes()

    assert auth.bootstrap_from_env() is False
    assert auth_path.read_bytes() == before
    assert auth.current_user(token) == user
    assert auth.authenticate_user("admin", "Admin123") is None


def test_default_bootstrap_keeps_changed_admin_password(isolated_auth_store) -> None:
    auth, auth_path, _ = isolated_auth_store
    assert auth.bootstrap_from_env() is True
    token, user = auth.authenticate_user("admin", "Admin123")
    assert auth.change_password(token, "Admin123", "changed-password")
    before = auth_path.read_bytes()

    assert auth.bootstrap_from_env() is False
    assert auth_path.read_bytes() == before
    assert auth.authenticate_user("admin", "Admin123") is None
    assert auth.authenticate_user("admin", "changed-password")[1] == user


def test_copied_initialization_template_preseeds_admin_and_smtp(
    isolated_auth_store, monkeypatch, tmp_path,
) -> None:
    from app import secrets_store
    from app.bootstrap_defaults import SMTP_CONFIG
    from app.services import email_adapter, preferences

    auth, _, env_path = isolated_auth_store
    template = Path(__file__).resolve().parents[2] / ".env.example"
    env_path.write_bytes(template.read_bytes())
    configured = Settings(_env_file=env_path, data_dir=tmp_path, multitenancy_enabled=False)
    monkeypatch.setattr(app_config, "settings", configured)
    preferences._invalidate_cache()
    secrets_store._cache = None
    secrets_store._cache_sig = None
    try:
        assert auth.bootstrap_from_env() is True
        assert auth.authenticate_user("admin", "Admin123") is not None
        smtp, password = email_adapter.get_registration_smtp("user@example.com")
        assert smtp == {**SMTP_CONFIG, "to_addresses": ["user@example.com"]}
        assert password == ""
        assert smtp["host"] == ""
        assert configured.local_admin_mode is False
    finally:
        preferences._invalidate_cache()
        secrets_store._cache = None
        secrets_store._cache_sig = None


def test_default_registration_secret_initializes_chinese_value_once(
    isolated_auth_store, monkeypatch,
) -> None:
    auth, auth_path, _ = isolated_auth_store
    monkeypatch.setattr(app_config.settings, "auth_registration_secret", "")
    assert auth.bootstrap_registration_secret_from_env() is True
    assert auth.verify_registration_secret("天王盖地虎") is True
    assert auth.verify_registration_secret("Admin123") is False
    assert "天王盖地虎" not in auth_path.read_text()
    before = auth_path.read_bytes()
    assert auth.bootstrap_registration_secret_from_env() is False
    assert auth_path.read_bytes() == before
