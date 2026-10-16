"""Full-product identity acceptance using isolated, real PostgreSQL databases."""
# ruff: noqa: E402 -- check the optional PostgreSQL driver before importing modules.
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row

from app.cell.admin import (
    bootstrap_full_identity,
    import_full_accounts,
    import_users,
    migrate,
    provision_tenant,
    register_cell,
    set_tenant_status,
)
from app.cell.config import CellSettings
from app.cell.context import AccessError
from app.cell.database import Databases
from app.cell.passwords import hash_password, token_hash
from app.multitenancy import access, bootstrap, routes
from app.multitenancy.identity import AccountIdentity
from app.services import email_adapter

SECRET = "沪深看盘注册口令"
PASSWORD = "full-tenant-password"


@pytest.fixture(scope="module")
def full_cluster():
    dsn = os.environ.get("TSP_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires a disposable PostgreSQL server")
    prefix = "tsp_full_" + uuid.uuid4().hex[:12]
    dbnames = [prefix + "_control", prefix + "_data"]
    role_names = [prefix + "_identity", prefix + "_business"]
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as root:
        try:
            for name in role_names:
                root.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS")
                             .format(sql.Identifier(name), sql.Literal(PASSWORD)))
            for name in dbnames:
                root.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            with ExitStack() as stack:
                admins = [stack.enter_context(psycopg.connect(
                    make_conninfo(dsn, dbname=name), autocommit=True, row_factory=dict_row,
                )) for name in dbnames]
                for kind, conn, role in zip(("control", "data"), admins, role_names, strict=True):
                    migrate(conn, kind, role, "full", target_version=6)
                register_cell(admins[0], "full", "https://full.example.test")
                bootstrap_full_identity(admins[0])
                settings = CellSettings(
                    id="full", schema_version=6, pool_size=8,
                    control_dsn=make_conninfo(dsn, dbname=dbnames[0], user=role_names[0], password=PASSWORD),
                    data_dsn=make_conninfo(dsn, dbname=dbnames[1], user=role_names[1], password=PASSWORD),
                )
                db = Databases(settings)
                db.open()
                stack.callback(db.close)
                identity = AccountIdentity(db)
                identity.set_registration_secret(SECRET)
                yield {"control": admins[0], "data": admins[1], "db": db, "identity": identity,
                       "roles": role_names}
        finally:
            for name in reversed(dbnames):
                root.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            for name in reversed(role_names):
                root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def env(full_cluster, monkeypatch):
    captured = {}

    def send(config, password, subject, body, **kwargs):
        captured[config["to_addresses"][0]] = re.search(r"\b[0-9]{6}\b", body).group()
        return True

    monkeypatch.setattr(email_adapter, "get_registration_smtp",
                        lambda email: ({"host": "mail.test", "from_address": "sender@example.test",
                                        "to_addresses": [email]}, "not-a-real-credential"))
    monkeypatch.setattr(email_adapter, "send_email", send)
    app = FastAPI()
    app.state.account_identity = full_cluster["identity"]
    from fastapi.responses import JSONResponse

    class PlatformGateway:
        async def forward(self, request):
            return JSONResponse({"admin": True})

    app.state.platform_admin = PlatformGateway()
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    app.include_router(routes.router)
    app.include_router(bootstrap.router)

    @app.get("/api/settings")
    def settings():
        return {"admin": True}

    @app.get("/api/data/status")
    def data():
        return {"admin": True}

    @app.get("/api/watchlist")
    def private(request: Request):
        return {"tenant_id": request.state.tenant_context.tenant_id}

    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        yield {**full_cluster, "client": client, "codes": captured}


def register(env, email=None):
    email = email or uuid.uuid4().hex + "@example.test"
    identity = env["identity"]
    identity.send_registration_code(email, SECRET, uuid.uuid4().hex)
    token, user = identity.register(email, PASSWORD, env["codes"][email])
    return token, user


def test_admin_username_no_fake_email_and_password_never_reset(env):
    identity = env["identity"]
    token, admin = identity.login("admin", "Admin123", uuid.uuid4().hex)
    assert admin["username"] == "admin" and admin["email"] is None
    assert admin["role"] == "admin" and admin["default_tenant_id"] is None
    assert "settings:manage" in admin["permissions"]
    identity.change_password(token, "Admin123", "UpdatedAdmin123")
    with pytest.raises(AccessError, match="会话已过期"):
        identity.current_user(token)
    assert bootstrap_full_identity(env["control"]) is False
    token, changed = AccountIdentity(env["db"]).login("admin", "UpdatedAdmin123", uuid.uuid4().hex)
    assert changed["id"] == admin["id"]
    identity.change_password(token, "UpdatedAdmin123", "Admin123")


def test_chinese_secret_is_required_before_any_email(env):
    with pytest.raises(AccessError, match="注册口令错误"):
        env["identity"].send_registration_code("wrong@example.test", "错误口令", uuid.uuid4().hex)
    assert not env["codes"]
    assert not env["control"].execute(
        "SELECT 1 FROM control.verification_codes WHERE email='wrong@example.test'"
    ).fetchone()
    with pytest.raises(AccessError, match="先发送验证码"):
        env["identity"].register("bypass@example.test", PASSWORD, "123456")


def test_register_creates_ready_personal_tenant_with_default_data(env):
    token, user = register(env)
    assert user["role"] == "tenant" and user["default_tenant_id"]
    assert user["permissions"] == ["account:manage", "business:read", "business:write"]
    tenant = env["control"].execute(
        "SELECT * FROM control.tenants WHERE id=%s", (user["default_tenant_id"],),
    ).fetchone()
    assert tenant["status"] == "active"
    job = env["control"].execute(
        "SELECT * FROM control.provisioning_jobs WHERE user_id=%s", (user["id"],),
    ).fetchone()
    assert job["state"] == "ready"
    context = env["identity"].context(user, None, "registration")
    with env["db"].business_transaction(context) as conn:
        assert conn.execute("SELECT value FROM business.documents").fetchone()["value"] == {}
    assert env["identity"].current_user(token)["id"] == user["id"]
    digest = env["control"].execute(
        "SELECT digest FROM control.sessions WHERE user_id=%s", (user["id"],),
    ).fetchone()["digest"]
    assert digest == token_hash(token) and digest != token


def test_code_is_consumed_once_and_duplicate_registration_is_bounded(env):
    email = uuid.uuid4().hex + "@example.test"
    env["identity"].send_registration_code(email, SECRET, uuid.uuid4().hex)
    code = env["codes"][email]

    def submit(_):
        try:
            return env["identity"].register(email, PASSWORD, code)[1]["id"]
        except AccessError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert sum(isinstance(result, str) for result in results) == 1
    assert results.count(409) == 3
    assert env["control"].execute(
        "SELECT count(*) AS n FROM control.users WHERE email=%s", (email,),
    ).fetchone()["n"] == 1


def test_expiry_attempts_cooldown_and_secret_rotation(env):
    email = uuid.uuid4().hex + "@example.test"
    identity = env["identity"]
    identity.send_registration_code(email, SECRET, uuid.uuid4().hex)
    with pytest.raises(AccessError, match="秒后重新发送"):
        identity.send_registration_code(email, SECRET, uuid.uuid4().hex)
    code = env["codes"][email]
    wrong = "000000" if code != "000000" else "000001"
    for _ in range(5):
        with pytest.raises(AccessError, match="验证码错误"):
            identity.register(email, PASSWORD, wrong)
    with pytest.raises(AccessError, match="错误次数过多"):
        identity.register(email, PASSWORD, code)
    env["control"].execute(
        "UPDATE control.verification_codes SET expires_at=now()-interval '1 second' WHERE email=%s", (email,),
    )
    with pytest.raises(AccessError, match="验证码已过期"):
        identity.register(email, PASSWORD, code)
    another = uuid.uuid4().hex + "@example.test"
    identity.send_registration_code(another, SECRET, uuid.uuid4().hex)
    identity.set_registration_secret(SECRET)
    with pytest.raises(AccessError, match="口令已更新"):
        identity.register(another, PASSWORD, env["codes"][another])


def test_password_recovery_is_purpose_bound_and_revokes_credentials(env):
    token, user = register(env)
    second_token, _ = env["identity"].login(user["email"], PASSWORD, uuid.uuid4().hex)
    old_version = user["auth_version"]
    token_id = uuid.uuid4().hex
    env["control"].execute(
        """INSERT INTO control.api_tokens
           (id,tenant_id,user_id,digest,name,permissions,policy_version,expires_at,
            full_scopes,auth_version,routing_epoch)
           SELECT %s,t.id,u.id,%s,'recovery-test',ARRAY['watchlist:read'],
                  t.policy_version,now()+interval '1 day',ARRAY['read:market'],
                  u.auth_version,t.routing_epoch
           FROM control.users u JOIN control.tenants t ON t.id=u.default_tenant_id
           WHERE u.id=%s""",
        (token_id, uuid.uuid4().hex, user["id"]),
    )

    wrong_secret = env["client"].post(
        "/api/auth/recovery/code",
        json={"email": user["email"], "registration_secret": "错误口令"},
    )
    assert wrong_secret.status_code == 403

    unknown_email = uuid.uuid4().hex + "@example.test"
    unknown = env["client"].post(
        "/api/auth/recovery/code",
        json={"email": unknown_email, "registration_secret": SECRET},
    )
    assert unknown.status_code == 200
    assert unknown_email not in env["codes"]
    unknown_record = env["control"].execute(
        """SELECT delivered FROM control.verification_codes
           WHERE email=%s AND purpose='registration'""",
        ("password-recovery " + unknown_email,),
    ).fetchone()
    assert unknown_record and not unknown_record["delivered"]

    sent = env["client"].post(
        "/api/auth/recovery/code",
        json={"email": user["email"], "registration_secret": SECRET},
    )
    assert sent.status_code == 200
    recovery_code = env["codes"][user["email"]]
    keys = env["control"].execute(
        """SELECT email FROM control.verification_codes
           WHERE email IN (%s,%s) ORDER BY email""",
        (user["email"], "password-recovery " + user["email"]),
    ).fetchall()
    assert {row["email"] for row in keys} == {
        user["email"],
        "password-recovery " + user["email"],
    }

    rejected = env["client"].post(
        "/api/auth/recovery/reset",
        json={
            "email": user["email"],
            "registration_secret": "错误口令",
            "code": recovery_code,
            "new_password": "recovered-password",
        },
    )
    assert rejected.status_code == 403
    wrong_code = "000000" if recovery_code != "000000" else "000001"
    invalid = env["client"].post(
        "/api/auth/recovery/reset",
        json={
            "email": user["email"],
            "registration_secret": SECRET,
            "code": wrong_code,
            "new_password": "recovered-password",
        },
    )
    assert invalid.status_code == 400
    reset = env["client"].post(
        "/api/auth/recovery/reset",
        json={
            "email": user["email"],
            "registration_secret": SECRET,
            "code": recovery_code,
            "new_password": "recovered-password",
        },
    )
    assert reset.status_code == 200

    with pytest.raises(AccessError, match="会话已过期"):
        env["identity"].current_user(token)
    with pytest.raises(AccessError, match="会话已过期"):
        env["identity"].current_user(second_token)
    with pytest.raises(AccessError, match="用户名/邮箱或密码错误"):
        env["identity"].login(user["email"], PASSWORD, uuid.uuid4().hex)
    _, recovered = env["identity"].login(
        user["email"],
        "recovered-password",
        uuid.uuid4().hex,
    )
    assert recovered["auth_version"] == old_version + 1
    assert env["control"].execute(
        "SELECT revoked_at IS NOT NULL AS revoked FROM control.api_tokens WHERE id=%s",
        (token_id,),
    ).fetchone()["revoked"]


def test_interrupted_two_database_creation_recovers_same_user_and_tenant(env):
    email = uuid.uuid4().hex + "@example.test"
    identity = env["identity"]
    identity.send_registration_code(email, SECRET, uuid.uuid4().hex)
    # Force a real failure after the data binding commit, during control activation.
    role = sql.Identifier(env["roles"][0])
    env["control"].execute(sql.SQL("REVOKE UPDATE ON control.users FROM {}").format(role))
    try:
        with pytest.raises(AccessError, match="初始化暂未完成"):
            identity.register(email, PASSWORD, env["codes"][email])
    finally:
        env["control"].execute(sql.SQL("GRANT UPDATE ON control.users TO {}").format(role))
    saved = env["control"].execute("SELECT * FROM control.users WHERE email=%s", (email,)).fetchone()
    assert saved["account_state"] == "pending" and not saved["active"]
    original_tenant = env["control"].execute(
        "SELECT tenant_id FROM control.provisioning_jobs WHERE user_id=%s", (saved["id"],),
    ).fetchone()["tenant_id"]
    assert env["data"].execute(
        "SELECT 1 FROM business.tenant_bindings WHERE tenant_id=%s", (original_tenant,),
    ).fetchone()
    token, user = identity.login(email, PASSWORD, uuid.uuid4().hex)
    assert user["id"] == saved["id"] and user["default_tenant_id"] == original_tenant
    assert identity.current_user(token)["id"] == saved["id"]
    assert identity.recover_pending() == 0


def test_runtime_and_real_rls_fail_closed(env):
    _, first = register(env)
    _, second = register(env)
    identity = env["identity"]
    with pytest.raises(AccessError, match="无权访问"):
        identity.context(first, second["default_tenant_id"], "forged-header")
    context = identity.context(first, None, "rls")
    with env["db"].business_transaction(context, write=True) as conn:
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute(
                """INSERT INTO business.documents(tenant_id,namespace,id,value)
                   VALUES(%s,'private','cross-tenant','{}')""", (second["default_tenant_id"],),
            )
        assert len(conn.execute("SELECT * FROM business.documents").fetchall()) == 1
    with env["db"].data.connection() as conn:
        assert conn.execute("SELECT * FROM business.documents").fetchall() == []
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO business.tenant_bindings VALUES('cannot-bootstrap-directly',1,true)"
            )
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("ALTER TABLE business.documents DISABLE ROW LEVEL SECURITY")
    with env["db"].control.connection() as conn, pytest.raises(InsufficientPrivilege), conn.transaction():
        conn.execute("ALTER TABLE control.users ADD COLUMN unsafe text")


def test_full_auth_contract_and_tenant_cannot_access_data_or_settings(env):
    token, account = register(env)
    client = env["client"]
    response = client.post("/api/auth/login", json={"email": account["email"], "password": PASSWORD})
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/auth/status").json()["user"]["role"] == "tenant"
    assert client.get("/api/watchlist").json()["tenant_id"] == account["default_tenant_id"]
    for path in ("/api/settings", "/api/data/status", "/api/admin/users"):
        assert client.get(path).status_code == 403
    _, other = register(env)
    assert client.get("/api/watchlist", headers={"X-Tenant-ID": other["default_tenant_id"]}).status_code == 403
    assert client.get("/api/watchlist", headers={"Authorization": "Bearer " + token}).status_code == 401
    assert client.get("/api/watchlist").headers["cache-control"] == "no-store"
    public = client.get("/api/auth/status").text
    assert "password_hash" not in public and "smtp" not in public and "secret_hash" not in public


def test_cross_origin_registration_and_role_forgery_are_denied(env):
    client = env["client"]
    assert client.post(
        "/api/auth/login", headers={"Origin": "https://hostile.test"},
        json={"identifier": "admin", "password": "Admin123"},
    ).status_code == 403
    assert client.post(
        "/api/auth/register", json={"email": "forge@example.test", "password": PASSWORD,
                                     "code": "123456", "role": "admin"},
    ).status_code == 422
    assert client.post("/api/auth/setup", json={"password": "Admin123"}).status_code == 401


def test_rbac_changes_and_suspension_invalidate_other_process_sessions(env):
    token, account = register(env)
    _, admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    another_process = AccountIdentity(env["db"])
    assert another_process.current_user(token)["role"] == "tenant"
    env["identity"].update_user(admin, account["id"], "tenant", False, "suspend-test")
    with pytest.raises(AccessError) as exc:
        another_process.current_user(token)
    assert exc.value.status == 401
    with pytest.raises(AccessError) as exc:
        another_process.login(account["email"], PASSWORD, uuid.uuid4().hex)
    assert exc.value.status == 401
    with pytest.raises(AccessError, match="至少一个有效管理员"):
        env["identity"].update_user(admin, admin["id"], "tenant", True, "last-admin")


def test_business_bootstrap_keeps_full_navigation_and_private_display_preferences(env):
    token, account = register(env)
    client = env["client"]
    client.cookies.set("tf_session", token)
    response = client.get("/api/business/bootstrap")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["menus"]) == 18 and "/" in payload["menus"] and "/data" not in payload["menus"]
    assert payload["settings_tabs"] == ["account"] and payload["business"]["onboarding_completed"]
    assert "smtp" not in response.text and "api_key" not in response.text and "base_url" not in response.text
    layout = {"v": 1, "items": [{"i": "mine", "t": "indices", "x": 0, "y": 0, "w": 4, "h": 8}]}
    assert client.put("/api/business/preferences/dashboard-layout", json={"layout": layout}).status_code == 200
    assert client.get("/api/business/preferences").json()["dashboard_layout"]["items"][0]["i"] == "mine"
    other_token, _ = register(env)
    client.cookies.set("tf_session", other_token)
    assert client.get("/api/business/preferences").json()["dashboard_layout"] is None
    assert client.put(
        "/api/business/preferences/feishu-webhook", json={"columns": []},
    ).status_code == 403
    assert env["identity"].current_user(token)["id"] == account["id"]


def test_smtp_failure_removes_undelivered_authorization(env, monkeypatch):
    email = uuid.uuid4().hex + "@example.test"
    monkeypatch.setattr(email_adapter, "send_email", lambda *args, **kwargs: False)
    with pytest.raises(AccessError, match="邮件发送失败"):
        env["identity"].send_registration_code(email, SECRET, uuid.uuid4().hex)
    assert not env["control"].execute(
        "SELECT 1 FROM control.verification_codes WHERE email=%s", (email,),
    ).fetchone()
    with pytest.raises(AccessError, match="先发送验证码"):
        env["identity"].register(email, PASSWORD, "123456")


def test_administrator_menu_changes_are_shared_without_overriding_personal_columns(env, monkeypatch):
    from psycopg.types.json import Jsonb

    from app.multitenancy.preferences import UserPreferences
    from app.services import preferences as platform

    token, admin = env["identity"].login("admin", "Admin123", uuid.uuid4().hex)
    monkeypatch.setattr("app.api.settings.get_preferences", lambda request: {"nav_order": [], "nav_hidden": []})
    monkeypatch.setattr(platform, "get_nav_order", lambda: ["indices", "watchlist", "dashboard"])
    monkeypatch.setattr(platform, "get_nav_hidden", lambda: ["financials"])
    env["control"].execute(
        """INSERT INTO control.user_preferences(user_id,value) VALUES(%s,%s)
           ON CONFLICT(user_id) DO UPDATE SET value=excluded.value""",
        (admin["id"], Jsonb({"nav_order": ["stale-personal"], "nav_hidden": [],
                            "watchlist_columns": [{"key": "mine", "visible": True}]})),
    )
    env["client"].cookies.set("tf_session", token)
    result = env["client"].get("/api/business/preferences").json()
    assert result["nav_order"] == ["indices", "watchlist", "dashboard"]
    assert result["nav_hidden"] == ["financials"]
    assert result["watchlist_columns"][0]["key"] == "mine"
    other_token, other = register(env)
    env["client"].cookies.set("tf_session", other_token)
    result = env["client"].get("/api/business/preferences").json()
    assert result["nav_order"] == ["indices", "watchlist", "dashboard"]
    assert result["watchlist_columns"] is None
    assert UserPreferences(env["db"]).load(other["id"])["nav_order"] == []


def test_legacy_account_import_retains_chinese_secret_role_and_explicit_ownership(env, tmp_path):
    salt, digest = hash_password(PASSWORD)
    users = {
        user_id: {"id": user_id, "email": user_id + "@example.test", "password_salt": salt,
                  "password_hash": digest, "created_at": 1700000000}
        for user_id in (uuid.uuid4().hex, uuid.uuid4().hex)
    }
    owner_id = next(iter(users))
    secret = env["control"].execute("SELECT * FROM control.registration_config").fetchone()
    (tmp_path / "auth.json").write_text(json.dumps({
        "users": users, "sessions": {"never-import": {}},
        "registration_secret_salt": secret["secret_salt"], "registration_secret_hash": secret["secret_hash"],
    }))
    (tmp_path / "preferences.json").write_text(json.dumps({
        "watchlist_columns": [{"key": "mine", "visible": True}],
        "email_smtp_config": {"password": "must-not-become-personal-preferences"},
    }))
    import_users(env["control"], tmp_path, owner_id)
    tenant_id = uuid.uuid4().hex
    provision_tenant(env["control"], env["data"], tenant_id, "Legacy", "full", owner_id)
    set_tenant_status(env["control"], tenant_id, "suspended", owner_id)
    result = import_full_accounts(env["control"], tmp_path, tenant_id, owner_id)
    assert result == {"users": 2, "registration_secret_imported": True, "preferences": 1}
    set_tenant_status(env["control"], tenant_id, "active", owner_id)
    token, user = env["identity"].login(users[owner_id]["email"], PASSWORD, uuid.uuid4().hex)
    assert user["role"] == "tenant" and user["default_tenant_id"] == tenant_id
    assert user["created_at"] == 1700000000
    assert "settings:manage" not in user["permissions"]
    assert not env["control"].execute(
        "SELECT 1 FROM control.sessions WHERE digest=%s", (token_hash("never-import"),),
    ).fetchone()
    assert env["identity"].current_user(token)["id"] == owner_id


def test_full_watchlist_preserves_original_group_order_date_and_concurrent_updates(env):
    from app.multitenancy.watchlist import Watchlist

    _, first = register(env)
    _, second = register(env)
    identity = env["identity"]
    own = Watchlist(env["db"], identity.context(first, None, "watchlist-contract"))
    other = Watchlist(env["db"], identity.context(second, None, "watchlist-isolation"))
    _, group = own.create_group("短线", "orange")
    _, another = own.create_group("观察", "sky")
    rows, added = own.add_batch(["000001.SZ", "600000.SH"], "首批", group_id=group["id"])
    assert added == 2 and [row["symbol"] for row in rows] == ["600000.SH", "000001.SZ"]
    dates = {row["symbol"]: row["added_at"] for row in rows}
    rows = own.move_to_top("000001.SZ")
    assert rows[0]["symbol"] == "000001.SZ" and rows[0]["added_at"] == dates["000001.SZ"]
    own.add_to_group("000001.SZ", another["id"])
    assert set(own.list_symbols()[0]["group_ids"]) == {group["id"], another["id"]}
    assert other.list_symbols() == [] and other.list_groups() == []
    with pytest.raises(ValueError, match="分组不存在"):
        other.add("000002.SZ", group_id=group["id"])
    with pytest.raises(KeyError):
        other.delete_group(group["id"])
    assert own.reorder_groups([another["id"], group["id"]])[0]["id"] == another["id"]
    revision = own.revision()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda symbol: own.add(symbol), ["000002.SZ", "000003.SZ", "600001.SH", "600002.SH"]))
    assert len(own.list_symbols()) == 6 and own.revision() == revision + 4
    groups, rows = own.delete_group(group["id"])
    assert len(groups) == 1 and len(rows) == 6
    assert own.clear() == 6 and other.revision() == 0


def test_original_watchlist_api_is_tenant_scoped_without_using_global_files(env, monkeypatch):
    from types import SimpleNamespace

    from app.api import watchlist as api_watchlist
    from app.services import watchlist as global_watchlist

    monkeypatch.setattr(global_watchlist, "list_symbols", lambda: pytest.fail("global private read"))
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.state.repo = SimpleNamespace(get_name_map=lambda symbols: {"000001.SZ": "平安银行"})
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    app.include_router(api_watchlist.router)
    token, _ = register(env)
    other_token, _ = register(env)
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", token)
        group = client.post("/api/watchlist/groups", json={"name": "测试", "color": "blue"}).json()["group"]
        response = client.post("/api/watchlist", json={"symbol": "000001.SZ", "group_id": group["id"]})
        assert response.status_code == 200, response.text
        assert response.json()["symbols"][0]["name"] == "平安银行"
        scope = client.get("/api/watchlist").headers["x-tsp-account-scope"]
        assert scope.startswith("tsp-v1:")
        client.cookies.set("tf_session", other_token)
        assert client.get("/api/watchlist", headers={"X-TSP-Account-Scope": scope}).status_code == 401
        assert client.get("/api/watchlist", params={"account_scope": scope}).status_code == 401
        assert client.get("/api/watchlist").json() == {"symbols": []}
        assert client.get("/api/watchlist/groups").json() == {"groups": []}
        assert client.delete(f"/api/watchlist/groups/{group['id']}").status_code == 404
        assert client.post(f"/api/watchlist/groups/{group['id']}/clear").status_code == 404


def test_private_documents_concurrency_revision_and_real_rls(env):
    from app.multitenancy.documents import TenantDocuments

    _, first = register(env)
    _, second = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(first, None, "documents"))
    other = TenantDocuments(env["db"], env["identity"].context(second, None, "other-documents"))
    own.put("research", "counter", {"n": 0}, expected_revision=0)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: own.mutate("research", "counter", lambda old: {"n": old["n"] + 1}),
                      range(30)))
    assert own.get("research", "counter") == {"n": 30}
    assert own.record("research", "counter")["revision"] == 31
    assert other.get("research", "counter") is None and other.values("research") == []
    with pytest.raises(AccessError) as stale:
        own.put("research", "counter", {"n": 0}, expected_revision=1)
    assert stale.value.status == 409 and own.get("research", "counter") == {"n": 30}
    collection = {row["id"]: row["revision"] for row in own.records("research")}
    own.put("research", "dependent", {"counter": "counter"})
    with pytest.raises(AccessError) as changed:
        own.delete("research", "counter", expected_collection=collection)
    assert changed.value.status == 409 and own.get("research", "counter") == {"n": 30}
    with env["db"].data.connection() as conn:
        assert conn.execute("SELECT * FROM business.documents WHERE namespace='research'").fetchall() == []


def test_original_config_signals_and_cache_use_sql_with_cross_day_reset(env, monkeypatch):
    from app.multitenancy.documents import TenantDocuments
    from app.services import strategy_cache
    from app.strategy import config, custom_signals

    _, first = register(env)
    _, second = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(first, None, "config"))
    other = TenantDocuments(env["db"], env["identity"].context(second, None, "other-config"))
    monkeypatch.setattr(config, "_path", lambda *args, **kwargs: pytest.fail("global override file"))
    monkeypatch.setattr(custom_signals, "_dir", lambda *args, **kwargs: pytest.fail("global signal file"))
    monkeypatch.setattr(strategy_cache, "_cache_path", lambda *args: pytest.fail("global cache file"))
    config.save_override(own, "low_reversal", {"name": "我的策略", "params": {"lookback": 25}})
    assert config.load_override(own, "low_reversal")["params"]["lookback"] == 25
    assert config.list_overrides(other) == {}
    custom_signals.save_one(own, {
        "id": "mine", "name": "价格条件", "kind": "entry", "conditions": [
            {"left": "close", "op": ">", "right": "10"}],
    })
    assert custom_signals.signal_names(own)["csg_mine"] == "价格条件"
    assert custom_signals.load_all(other) == [] and not custom_signals.delete_one(other, "mine")

    def cache(sid):
        strategy_cache.write_cache(own, "2026-10-13", {
            sid: {"rows": [{"symbol": sid}], "total": 1, "as_of": "2026-10-13"},
        })

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(cache, ["a", "b", "c", "d"]))
    assert set(strategy_cache.read_cache(own)["results"]) == {"a", "b", "c", "d"}
    assert strategy_cache.read_cache(other) is None
    strategy_cache.write_cache(own, "2026-10-14", {"a": {"rows": [], "total": 0}})
    assert strategy_cache.read_cache(own)["today_ever_matched"] == {"a": []}
    assert set(strategy_cache.read_cache(own)["results"]) == {"a"}


def test_factor_registry_concurrent_accounts_and_old_global_registry_hidden(env, monkeypatch, tmp_path):
    from app.api import factors
    from app.factors import registry, store
    from app.multitenancy.documents import TenantDocuments

    first_token, first = register(env)
    second_token, second = register(env)
    public = registry.FactorSpec(id="uf_old_global", label="旧用户", group="旧", formula_text="close", kind="custom")
    monkeypatch.setitem(registry._REGISTRY, public.id, public)
    monkeypatch.setattr(store, "_dir", lambda *args: pytest.fail("global factor directory"))
    monkeypatch.setattr(factors, "_trial_nonempty", lambda *args, **kwargs: None)
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    app.include_router(factors.router)
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", first_token)
        assert client.post("/api/factors/custom", json={
            "id": "uf_same", "label": "第一位", "formula": "close * 2",
        }).status_code == 200
        assert client.post("/api/factors/composite", json={
            "id": "cf_mine", "label": "组合", "members": {"uf_same": 1, "rsi_14": 1},
        }).status_code == 200
        client.cookies.set("tf_session", second_token)
        assert "uf_same" not in {row["id"] for row in client.get("/api/factors").json()["factors"]}
        assert client.post("/api/factors/custom", json={
            "id": "uf_same", "label": "第二位", "formula": "close * 3",
        }).status_code == 200
        assert client.delete("/api/factors/custom/cf_mine").status_code == 404
    assert registry.get_factor("uf_same") is None and registry.get_factor(public.id) is public

    def factor_snapshot(account):
        documents = TenantDocuments(env["db"], env["identity"].context(account, None, "factor-snapshot"))
        with registry.private_factor_registry(documents.values("custom_factors")):
            assert registry.get_factor("uf_old_global") is None
            return registry.get_factor("uf_same").formula_text

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(factor_snapshot, [first, second])) == ["close * 2", "close * 3"]


def test_original_strategy_api_source_rbac_and_config_are_tenant_scoped(env, monkeypatch, tmp_path):
    from dataclasses import replace
    from pathlib import Path
    from types import SimpleNamespace

    from app.api import screener, signals, strategy
    from app.multitenancy.strategies import definition_payload
    from app.strategy.ai_generator import AIStrategyGenerator
    from app.strategy.engine import StrategyEngine

    builtins = StrategyEngine([Path(strategy.__file__).resolve().parents[1] / "strategy" / "builtin"])
    template = builtins.strategy_definitions()[0]
    seen = []

    class MetadataOnlyExecutor:
        def execute(self, payload):
            assert payload["kind"] == "describe"
            seen.append(payload)
            target = next(row for row in payload["sources"] if row["id"] == payload["strategy_id"])
            meta = AIStrategyGenerator._extract_meta(target["code"])
            return {"definition": definition_payload(replace(template, meta=meta, source=target["source"],
                                                             required_features=frozenset()))}

    # After trusted builtins are loaded, every import of a strategy file is a
    # test failure. The API must only transfer tenant code to the executor.
    monkeypatch.setattr(StrategyEngine, "_load_file", lambda *args: pytest.fail("API executed private code"))
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.state.strategy_engine = builtins
    app.state.compute_executor = MetadataOnlyExecutor()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    for router in (strategy.router, signals.router, screener.router):
        app.include_router(router)
    first_token, first = register(env)
    second_token, _ = register(env)
    code = """import polars as pl
META = {"id": "custom_private", "name": "个人策略", "description": "", "tags": [],
        "params": [], "scoring": {}, "asset_types": ["stock"], "timeframes": ["1d"]}
def filter(df, params):
    return pl.col("close") > 10
"""
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", first_token)
        response = client.post("/api/strategies/code/save", json={
            "strategy_id": "custom_private", "code": code, "mode": "create", "target_source": "custom",
        })
        assert response.status_code == 200, response.text
        assert seen[0]["tenant_id"] == first["default_tenant_id"]
        assert client.get("/api/strategies/custom_private/source").json()["code"] == code
        response = client.post("/api/strategies/config", json={
            "strategy_id": template.meta["id"], "overrides": {"name": "个人覆盖"},
        })
        assert response.status_code == 200, response.text
        presets = client.get("/api/screener/strategies").json()["presets"]
        assert next(row for row in presets if row["id"] == template.meta["id"])["name"] == "个人覆盖"
        assert client.post("/api/custom-signals", json={
            "id": "own", "name": "个人条件", "kind": "entry",
            "conditions": [{"left": "close", "op": ">", "right": "10"}],
        }).status_code == 200
        client.cookies.set("tf_session", second_token)
        assert client.get("/api/strategies/custom_private/source").status_code == 404
        assert client.delete("/api/strategies/custom_private").status_code == 404
        assert client.get("/api/custom-signals").json() == {"signals": []}
        presets = client.get("/api/screener/strategies").json()["presets"]
        assert next(row for row in presets if row["id"] == template.meta["id"])["name"] == template.meta["name"]
        client.cookies.set("tf_session", first_token)
        assert client.delete("/api/strategies/custom_private").status_code == 200
    assert list(tmp_path.iterdir()) == []
