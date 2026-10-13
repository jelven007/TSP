"""Real PostgreSQL isolation tests. Use a disposable superuser database server."""

# ruff: noqa: E402 -- optional driver must be checked before importing Cell modules

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege
from psycopg.rows import dict_row

from app.cell.admin import (
    add_user,
    import_legacy,
    import_users,
    migrate,
    provision_tenant,
    register_cell,
    set_tenant_status,
)
from app.cell.config import CellSettings
from app.cell.context import ROLE_PERMISSIONS, AccessError, Role, TenantContext
from app.cell.database import Databases
from app.cell.main import create_app
from app.cell.passwords import hash_password, token_hash

PASSWORD = "test-cell-password"


@pytest.fixture(scope="module")
def cluster():
    admin_dsn = os.environ.get("TSP_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("TSP_TEST_POSTGRES_DSN must point to a disposable PostgreSQL server")
    prefix = "tsp_test_" + uuid.uuid4().hex[:12]
    databases = [prefix + suffix for suffix in ("_control", "_a", "_b")]
    roles = [prefix + suffix for suffix in ("_identity", "_data_a", "_data_b")]
    with psycopg.connect(admin_dsn, autocommit=True, row_factory=dict_row) as root:
        try:
            for role in roles:
                root.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE").format(
                        sql.Identifier(role), sql.Literal(PASSWORD)
                    )
                )
            for database in databases:
                root.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            with ExitStack() as stack:
                admins = [
                    stack.enter_context(psycopg.connect(
                        make_conninfo(admin_dsn, dbname=database), autocommit=True, row_factory=dict_row
                    )) for database in databases
                ]
                control, data_a, data_b = admins
                migrate(control, "control", roles[0])
                migrate(data_a, "data", roles[1], "a")
                migrate(data_b, "data", roles[2], "b")
                settings = [
                    CellSettings(
                        id=cell,
                        control_dsn=make_conninfo(admin_dsn, dbname=databases[0], user=roles[0], password=PASSWORD),
                        data_dsn=make_conninfo(admin_dsn, dbname=database, user=role, password=PASSWORD),
                        pool_size=2,
                    )
                    for cell, database, role in zip(("a", "b"), databases[1:], roles[1:], strict=True)
                ]
                for cell in ("a", "b", "c"):
                    register_cell(control, cell, f"https://{cell}.example.test")
                users = {name: add_user(control, f"{name}@example.test", PASSWORD)
                         for name in ("owner", "other", "viewer", "admin", "member")}
                yield {
                    "control": control, "data": {"a": data_a, "b": data_b}, "settings": settings,
                    "users": users, "roles": roles, "admin_dsn": admin_dsn,
                }
        finally:
            for database in reversed(databases):
                root.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
            for role in reversed(roles):
                root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
def env(cluster):
    suffix = uuid.uuid4().hex[:8]
    tenants = {name: f"{name}-{suffix}" for name in ("alpha", "beta", "remote")}
    for name, cell, owner in (("alpha", "a", "owner"), ("beta", "a", "other"), ("remote", "b", "owner")):
        provision_tenant(cluster["control"], cluster["data"][cell], tenants[name], name, cell, cluster["users"][owner])
    for name, role in (("viewer", "viewer"), ("admin", "admin"), ("member", "member")):
        cluster["control"].execute(
            "INSERT INTO control.memberships VALUES (%s, %s, %s)",
            (tenants["alpha"], cluster["users"][name], role),
        )
    with ExitStack() as stack:
        clients = {cell: stack.enter_context(TestClient(create_app(settings)))
                   for cell, settings in zip(("a", "b"), cluster["settings"], strict=True)}
        tokens = {}
        for name in cluster["users"]:
            response = clients["a"].post("/api/auth/login", json={"email": f"{name}@example.test", "password": PASSWORD})
            assert response.status_code == 200, response.text
            tokens[name] = response.json()["token"]
        yield {**cluster, "clients": clients, "tenants": tenants, "tokens": tokens}


def headers(env, name="owner", *, epoch=1, token=None):
    return {"Authorization": "Bearer " + (token or env["tokens"][name]), "X-Routing-Epoch": str(epoch)}


def path(env, tenant="alpha", suffix="/watchlist"):
    return f"/api/tenants/{env['tenants'][tenant]}{suffix}"


def test_wrong_cell_stale_epoch_and_membership(env):
    client = env["clients"]["a"]
    assert client.get(path(env), headers=headers(env)).status_code == 200
    assert client.get(path(env), headers=headers(env, "other")).status_code == 403
    assert client.get(path(env, "beta"), headers=headers(env)).status_code == 403
    assert client.get(path(env, "remote"), headers=headers(env)).json()["detail"] == "wrong_cell"
    assert env["clients"]["b"].get(path(env, "remote"), headers=headers(env)).status_code == 200
    assert client.get(path(env), headers=headers(env, epoch=2)).json()["detail"] == "routing_epoch_stale"
    assert client.get(path(env), headers={"Authorization": "Bearer " + env["tokens"]["owner"]}).status_code == 422
    assert client.get(path(env), headers={"X-Tenant-ID": env["tenants"]["alpha"], "X-Routing-Epoch": "1"}).status_code == 401


def test_watchlist_isolation_and_cross_tenant_group_reference(env):
    client = env["clients"]["a"]
    group = client.post(path(env, suffix="/watchlist/groups"), headers=headers(env), json={"name": "核心"}).json()["group_id"]
    response = client.post(path(env), headers=headers(env),
                           json={"symbols": ["000001.SZ"], "note": "alpha-private", "group_ids": [group]})
    assert response.status_code == 200, response.text
    assert response.json()["symbols"][0]["group_ids"] == [group]
    other = client.post(path(env, "beta"), headers=headers(env, "other"),
                        json={"symbols": ["000001.SZ"], "note": "beta-private"})
    assert other.status_code == 200
    assert client.get(path(env), headers=headers(env)).json()["symbols"][0]["note"] == "alpha-private"
    assert client.get(path(env, "beta"), headers=headers(env, "other")).json()["symbols"][0]["note"] == "beta-private"
    assert client.post(path(env, "beta"), headers=headers(env, "other"),
                       json={"symbols": ["600000.SH"], "group_ids": [group]}).status_code == 404
    assert client.delete(path(env, suffix=f"/watchlist/groups/{group}"), headers=headers(env)).status_code == 200
    assert client.get(path(env), headers=headers(env)).json()["symbols"][0]["group_ids"] == []
    assert client.post(path(env), headers=headers(env), json={"symbols": ["688001.SH", "689001.SH"]}).status_code == 200
    client.delete(path(env), headers=headers(env))
    assert len(client.get(path(env, "beta"), headers=headers(env, "other")).json()["symbols"]) == 1


@pytest.mark.parametrize("name,write,manage", [
    ("owner", 200, 200), ("admin", 200, 200), ("member", 200, 403), ("viewer", 403, 403),
])
def test_role_permissions(env, name, write, manage):
    client = env["clients"]["a"]
    assert client.get(path(env), headers=headers(env, name)).status_code == 200
    assert client.post(path(env), headers=headers(env, name), json={"symbols": ["600000.SH"]}).status_code == write
    assert client.get(path(env, suffix="/members"), headers=headers(env, name)).status_code == manage


def test_last_owner_and_admin_cannot_grant_owner(env):
    client = env["clients"]["a"]
    owner_path = path(env, suffix="/members/" + env["users"]["owner"])
    assert client.delete(owner_path, headers=headers(env)).json()["detail"] == "last_owner"
    assert client.put(owner_path, headers=headers(env), json={"role": "admin"}).json()["detail"] == "last_owner"
    assert client.put(owner_path, headers=headers(env, "admin"), json={"role": "viewer"}).status_code == 403
    assert client.put(path(env, suffix="/members/" + env["users"]["member"]), headers=headers(env, "admin"),
                      json={"role": "owner"}).status_code == 403
    assert client.put(path(env, suffix="/members/" + env["users"]["admin"]), headers=headers(env),
                      json={"role": "owner"}).status_code == 204
    assert client.put(owner_path, headers=headers(env), json={"role": "member"}).status_code == 204
    assert client.get(path(env, suffix="/members"), headers=headers(env)).status_code == 403


def test_token_binding_scopes_revocation_and_policy_version(env):
    client = env["clients"]["a"]
    response = client.post(path(env, suffix="/tokens"), headers=headers(env),
                           json={"name": "read", "permissions": ["watchlist:read"]})
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    token_id = response.json()["id"]
    assert client.get(path(env), headers=headers(env, token=token)).status_code == 200
    assert client.post(path(env), headers=headers(env, token=token), json={"symbols": ["600000.SH"]}).status_code == 403
    assert client.get(path(env, "beta"), headers=headers(env, token=token)).json()["detail"] == "token_tenant_mismatch"
    assert client.get(path(env, suffix="/tokens"), headers=headers(env, token=token)).status_code == 403
    assert client.post(path(env, suffix="/tokens"), headers=headers(env),
                       json={"name": "bad", "permissions": ["members:manage"]}).status_code == 403
    stored = env["control"].execute("SELECT digest FROM control.api_tokens WHERE id = %s", (token_id,)).fetchone()["digest"]
    assert stored == token_hash(token) and stored != token
    listing = client.get(path(env, suffix="/tokens"), headers=headers(env)).json()
    assert "digest" not in str(listing) and token not in str(listing)
    assert client.delete(path(env, suffix="/tokens/" + token_id), headers=headers(env)).status_code == 204
    assert client.get(path(env), headers=headers(env, token=token)).status_code == 401
    token = client.post(path(env, suffix="/tokens"), headers=headers(env, "member"),
                        json={"name": "writer", "permissions": ["watchlist:write"]}).json()["token"]
    assert client.put(path(env, suffix="/members/" + env["users"]["member"]), headers=headers(env),
                      json={"role": "viewer"}).status_code == 204
    assert client.post(path(env), headers=headers(env, token=token), json={"symbols": ["600000.SH"]}).status_code == 401
    assert client.post(path(env), headers=headers(env, "member"), json={"symbols": ["600000.SH"]}).status_code == 403


def test_suspension_removed_member_and_logout(env):
    client = env["clients"]["a"]
    assert client.delete(path(env, suffix="/members/" + env["users"]["viewer"]), headers=headers(env)).status_code == 204
    assert client.get(path(env), headers=headers(env, "viewer")).status_code == 403
    env["control"].execute("UPDATE control.tenants SET status = 'suspended' WHERE id = %s", (env["tenants"]["alpha"],))
    assert client.get(path(env), headers=headers(env)).json()["detail"] == "tenant_unavailable"
    assert client.post("/api/auth/logout", headers=headers(env)).status_code == 204
    assert client.get("/api/tenants", headers=headers(env)).status_code == 401


def test_directory_stable_placement_and_unmigrated_routes(env):
    client = env["clients"]["a"]
    tenants = client.get("/api/tenants", headers=headers(env)).json()["tenants"]
    own = next(t for t in tenants if t["id"] == env["tenants"]["alpha"])
    assert own["cell_id"] == "a" and own["routing_epoch"] == 1
    assert not any(t["id"] == env["tenants"]["beta"] for t in tenants)
    register_cell(env["control"], "new-cell", "https://new.example.test")
    current = client.get("/api/tenants", headers=headers(env)).json()["tenants"]
    assert next(t for t in current if t["id"] == own["id"])["cell_id"] == "a"
    for route in ("/api/watchlist", "/api/settings", "/api/backtest", "/api/paper"):
        assert client.get(route, headers=headers(env)).status_code == 404
    assert client.get(path(env), headers=headers(env)).headers["cache-control"] == "no-store"


def test_login_rate_limit_and_concurrent_sessions(env):
    client = env["clients"]["a"]
    email = uuid.uuid4().hex + "@example.test"
    for _ in range(10):
        assert client.post("/api/auth/login", json={"email": email, "password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": email, "password": "bad"}).status_code == 429
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(
            lambda _: client.post("/api/auth/login", json={"email": "owner@example.test", "password": PASSWORD}),
            range(8),
        ))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["token"] for response in responses}) == 8


def test_group_reorder_assignment_and_rollback(env):
    client = env["clients"]["a"]
    groups_url = path(env, suffix="/watchlist/groups")
    first = client.post(groups_url, headers=headers(env), json={"name": "第一组"}).json()["group_id"]
    second = client.post(groups_url, headers=headers(env), json={"name": "第二组"}).json()["group_id"]
    assert client.post(groups_url, headers=headers(env), json={"name": "第一组"}).status_code == 409
    assert client.put(groups_url + "/reorder", headers=headers(env), json={"ordered_ids": [first, first]}).status_code == 422
    response = client.put(groups_url + "/reorder", headers=headers(env), json={"ordered_ids": [second, first]})
    assert [row["id"] for row in response.json()["groups"]] == [second, first]
    assert client.put(groups_url + "/" + first, headers=headers(env),
                      json={"name": "改名", "color": "rose"}).status_code == 200
    client.post(path(env), headers=headers(env), json={"symbols": ["600000.SH"], "group_ids": [first]})
    assignment = path(env, suffix="/watchlist/items/600000.SH/groups")
    assert client.put(assignment, headers=headers(env), json={"group_ids": [second]}).status_code == 200
    assert client.get(path(env), headers=headers(env)).json()["symbols"][0]["group_ids"] == [second]
    assert client.post(path(env), headers=headers(env),
                       json={"symbols": ["000001.SZ"], "group_ids": ["missing"]}).status_code == 404
    assert len(client.get(path(env), headers=headers(env)).json()["symbols"]) == 1
    assert client.delete(path(env, suffix="/watchlist/items/600000.SH"), headers=headers(env)).status_code == 200
    assert client.get(path(env), headers=headers(env)).json()["symbols"] == []
    assert client.get("/health/ready").status_code == 200


def test_real_rls_no_context_cross_write_fk_and_pool_reset(env):
    client = env["clients"]["a"]
    client.post(path(env), headers=headers(env), json={"symbols": ["000001.SZ"]})
    client.post(path(env, "beta"), headers=headers(env, "other"), json={"symbols": ["600000.SH"]})
    db = client.app.state.databases
    with db.data.connection() as conn:
        assert conn.execute("SELECT * FROM business.watchlist_items").fetchall() == []
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO business.watchlist_items(tenant_id, symbol) VALUES (%s, '000002.SZ')",
                (env["tenants"]["alpha"],),
            )
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (env["tenants"]["alpha"],))
            assert len(conn.execute("SELECT * FROM business.watchlist_items").fetchall()) == 1
            assert conn.execute(
                "DELETE FROM business.watchlist_items WHERE tenant_id = %s", (env["tenants"]["beta"],)
            ).rowcount == 0
            with pytest.raises(InsufficientPrivilege), conn.transaction():
                conn.execute(
                    "INSERT INTO business.watchlist_items VALUES (%s, '000002.SZ', '', now())", (env["tenants"]["beta"],)
                )
            with pytest.raises(ForeignKeyViolation), conn.transaction():
                conn.execute(
                    "INSERT INTO business.watchlist_group_items VALUES (%s, 'foreign-group', '000001.SZ')",
                    (env["tenants"]["alpha"],),
                )
        assert conn.execute("SELECT * FROM business.watchlist_items").fetchall() == []
        with pytest.raises(RuntimeError), conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (env["tenants"]["beta"],))
            raise RuntimeError("force rollback")
        assert conn.execute("SELECT * FROM business.watchlist_items").fetchall() == []
        for statement in ("TRUNCATE business.watchlist_items", "ALTER TABLE business.watchlist_items DISABLE ROW LEVEL SECURITY"):
            with pytest.raises(InsufficientPrivilege), conn.transaction():
                conn.execute(statement)
    assert client.get(path(env), headers=headers(env)).json()["symbols"][0]["symbol"] == "000001.SZ"


def test_two_cells_concurrent_requests(env):
    def run(index):
        remote = index % 2 == 1
        client = env["clients"]["b" if remote else "a"]
        target = "remote" if remote else "alpha"
        note = "remote-private" if remote else "alpha-private"
        response = client.post(path(env, target), headers=headers(env),
                               json={"symbols": ["000001.SZ"], "note": note})
        assert response.status_code == 200, response.text
        rows = client.get(path(env, target), headers=headers(env)).json()["symbols"]
        assert rows[0]["note"] == note
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(run, range(12)))


def test_data_identity_binding_epoch_and_unsafe_role(env):
    settings = env["settings"][0]
    bad = Databases(settings.model_copy(update={"id": "b"}))
    with pytest.raises(RuntimeError, match="different Cell"):
        bad.open()
    env["data"]["a"].execute(
        "UPDATE business.tenant_bindings SET routing_epoch = 2 WHERE tenant_id = %s", (env["tenants"]["alpha"],)
    )
    assert env["clients"]["a"].get(path(env), headers=headers(env)).json()["detail"] == "routing_epoch_stale"
    bad = Databases(CellSettings(
        id="a", control_dsn=make_conninfo(env["admin_dsn"], dbname=env["control"].info.dbname),
        data_dsn=settings.data_dsn,
    ))
    with pytest.raises(RuntimeError, match="non-owner"):
        bad.open()
    context = TenantContext(env["tenants"]["remote"], env["users"]["owner"], "b", Role.OWNER,
                            ROLE_PERMISSIONS[Role.OWNER], 1, 1, "test")
    with pytest.raises(AccessError, match="wrong_cell"), env["clients"]["a"].app.state.databases.business_transaction(context):
        pytest.fail("wrong cell accepted")


def test_migration_idempotence_and_import_legacy(cluster, tmp_path):
    control = cluster["control"]
    data = cluster["data"]["a"]
    migrate(control, "control", cluster["roles"][0])
    migrate(data, "data", cluster["roles"][1], "a")
    suffix = uuid.uuid4().hex
    salt, digest = hash_password(PASSWORD)
    user = {"id": suffix, "email": suffix + "@example.test", "password_salt": salt, "password_hash": digest}
    (tmp_path / "auth.json").write_text(json.dumps({"users": {suffix: user}, "sessions": {"must-not-import": {}}}))
    (tmp_path / "watchlist_groups.json").write_text(json.dumps([{"id": "g", "name": "旧分组"}]))
    (tmp_path / "alerts.jsonl").write_text(
        json.dumps({"ts": 1791422755310, "source": "legacy", "message": "旧告警"}) + "\n"
    )
    import polars as pl

    pl.DataFrame({"symbol": ["000001.SZ"], "note": ["legacy-private"], "group_id": ["g"],
                  "added_at": ["2026-10-12T10:00:00"]}).write_parquet(tmp_path / "watchlist.parquet")
    import_users(control, tmp_path, suffix)
    tenant_id = "legacy-" + suffix
    provision_tenant(control, data, tenant_id, "legacy", "a", suffix)
    with pytest.raises(ValueError, match="suspended"):
        import_legacy(control, data, tmp_path, tenant_id, suffix)
    set_tenant_status(control, tenant_id, "suspended", suffix)
    counts = import_legacy(control, data, tmp_path, tenant_id, suffix)
    assert counts == {"users": 1, "groups": 1, "items": 1, "alerts": 1}
    with pytest.raises(ValueError, match="empty"):
        import_legacy(control, data, tmp_path, tenant_id, suffix)
    set_tenant_status(control, tenant_id, "active", suffix)
    with TestClient(create_app(cluster["settings"][0])) as client:
        response = client.post("/api/auth/login", json={"email": user["email"], "password": PASSWORD})
        assert response.status_code == 200
        rows = client.get(f"/api/tenants/{tenant_id}/watchlist", headers={
            "Authorization": "Bearer " + response.json()["token"], "X-Routing-Epoch": "1",
        }).json()["symbols"]
        assert rows[0]["note"] == "legacy-private" and rows[0]["group_ids"] == ["g"]
        assert rows[0]["added_at"].startswith("2026-10-12")
        alerts = client.get(f"/api/tenants/{tenant_id}/alerts", headers={
            "Authorization": "Bearer " + response.json()["token"], "X-Routing-Epoch": "1",
        }).json()["alerts"]
        assert alerts[0]["event"]["message"] == "旧告警"
    assert not control.execute("SELECT 1 FROM control.sessions WHERE digest = %s", (token_hash("must-not-import"),)).fetchone()
