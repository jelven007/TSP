"""Real PostgreSQL scheduler fairness, quotas, wake-up races and replica recovery."""

# ruff: noqa: F811 -- pytest fixtures and optional dependency
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("psycopg")
from psycopg.errors import InsufficientPrivilege
from test_cell_isolation import cluster, env, headers, path  # noqa: F401

from app.cell.identity import Identity
from app.cell.scheduler import Scheduler
from app.cell.worker import CellWorker


def test_atomic_wake_rls_and_no_idle_scan(env):
    db = env["clients"]["a"].app.state.databases
    scheduler = Scheduler(db)
    # Prior tests may leave work in the module fixture; these claims are scoped by ID.
    tenant = env["tenants"]["alpha"]
    assert not env["data"]["a"].execute(
        "SELECT 1 FROM scheduler.work WHERE tenant_id=%s", (tenant,)
    ).fetchone()
    client = env["clients"]["a"]
    body = {"name": "wake", "symbols": ["000001.SZ"], "field": "price", "op": ">", "value": 9}
    saved = client.post(path(env, suffix="/monitor-rules"), headers=headers(env), json=body).json()["rule"]
    work = env["data"]["a"].execute("SELECT * FROM scheduler.work WHERE tenant_id=%s", (tenant,)).fetchone()
    assert work["kind"] == "realtime" and work["due_at"]
    with db.data.connection() as conn:
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT * FROM scheduler.work")
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE scheduler.slots SET slot=100")
    env["data"]["a"].execute("UPDATE scheduler.work SET due_at=NULL WHERE tenant_id!=%s", (tenant,))
    claim = scheduler.claim("realtime", 1)[0]
    assert claim["tenant_id"] == tenant
    # A business write after claim must not be erased by finish(None).
    client.post(path(env, suffix="/monitor-rules"), headers=headers(env), json={**body, "id": saved["id"], "enabled": False})
    assert scheduler.finish(claim, None)
    new = scheduler.claim("realtime", 1)
    assert len(new) == 1
    assert scheduler.finish(new[0], None)
    assert scheduler.claim("realtime", 1) == []


def test_replica_quota_expiry_fencing_and_fairness(env):
    db = env["clients"]["a"].app.state.databases
    data = env["data"]["a"]
    data.execute("DELETE FROM scheduler.work")
    data.execute("UPDATE scheduler.slots SET owner=NULL,lease_until=NULL,tenant_id=NULL")
    # No payloads are accessed in scheduler claims; admin fixtures seed 10 due tenants.
    for i in range(10):
        data.execute("INSERT INTO scheduler.work VALUES (%s,'strategies',now(),1,NULL,NULL)", (f"fair-{i:02d}",))

    def claim(_):
        return Scheduler(db).claim("strategies", 4)

    with ThreadPoolExecutor(max_workers=4) as executor:
        batches = list(executor.map(claim, range(4)))
    claimed = [row for batch in batches for row in batch]
    assert len(claimed) == 4 and len({r["tenant_id"] for r in claimed}) == 4
    assert {r["tenant_id"] for r in claimed} == {f"fair-{i:02d}" for i in range(4)}
    expired = claimed[0]
    data.execute("UPDATE scheduler.work SET lease_until=now()-interval '1 second' WHERE tenant_id=%s", (expired["tenant_id"],))
    data.execute("UPDATE scheduler.slots SET lease_until=now()-interval '1 second' WHERE tenant_id=%s", (expired["tenant_id"],))
    replacement = Scheduler(db).claim("strategies", 1)[0]
    assert replacement["tenant_id"] == expired["tenant_id"] and replacement["owner"] != expired["owner"]
    assert not Scheduler(db).finish(expired, None)
    assert Scheduler(db).finish(replacement, datetime.now(UTC) + timedelta(seconds=30))
    following = Scheduler(db).claim("strategies", 1)[0]
    assert following["tenant_id"] == "fair-04"
    assert Scheduler(db).finish(following, None)
    for row in claimed[1:]:
        assert Scheduler(db).finish(row, None)


def test_batch_quotes_one_request_and_disable_removes_work(env):
    db, data = env["clients"]["a"].app.state.databases, env["data"]["a"]
    data.execute("DELETE FROM scheduler.work")
    data.execute("UPDATE scheduler.slots SET owner=NULL,lease_until=NULL,tenant_id=NULL")
    client = env["clients"]["a"]
    for name, user in (("alpha", "owner"), ("beta", "other")):
        assert client.post(path(env, name, "/monitor-rules"), headers=headers(env, user), json={
            "name": "合并", "symbols": ["000001.SZ"], "field": "price", "op": ">", "value": 9,
        }).status_code == 200
    from test_cell_business import Market

    class CountMarket(Market):
        calls = 0
        def quotes(self, symbols):
            self.calls += 1
            return super().quotes(symbols)
    market = CountMarket()
    assert CellWorker(db, market).tick() == 2
    assert market.calls == 1
    assert client.get(path(env, suffix="/alerts"), headers=headers(env)).json()["alerts"]
    assert client.get(path(env, "beta", "/alerts"), headers=headers(env, "other")).json()["alerts"]
    with Identity(db).worker_context(env["tenants"]["alpha"]) as (ctx, _), db.business_transaction(
        ctx, write=True, permission="alerts:write"
    ) as conn:
        conn.execute("UPDATE business.monitor_rules SET enabled=false")
    # Drain the wake caused during evaluate then verify the disabled tenant is parked.
    time.sleep(.01)
    CellWorker(db, market).tick()
    assert data.execute(
        "SELECT due_at FROM scheduler.work WHERE tenant_id=%s AND kind='realtime'",
        (env["tenants"]["alpha"],),
    ).fetchone()["due_at"] is None


def test_v2_upgrade_backfills_with_non_superuser_migration_owner(cluster):
    import hashlib
    import uuid

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row

    from app.cell.admin import SQL_ROOT, migrate

    prefix = "tsp_upgrade_" + uuid.uuid4().hex[:10]
    database, owner, runtime = prefix + "_db", prefix + "_owner", prefix + "_runtime"
    with psycopg.connect(cluster["admin_dsn"], autocommit=True, row_factory=dict_row) as root:
        try:
            root.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'test-upgrade' NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(owner)))
            root.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'test-upgrade' NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(runtime)))
            root.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(runtime), sql.Identifier(owner)))
            root.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(owner)))
            with psycopg.connect(make_conninfo(cluster["admin_dsn"], dbname=database, user=owner, password="test-upgrade"),
                                 autocommit=True, row_factory=dict_row) as conn:
                for version in (1, 2):
                    source = (SQL_ROOT / f"data_v{version}.sql").read_text()
                    conn.execute(source)
                    conn.execute("INSERT INTO business.schema_version VALUES (%s,%s)",
                                 (version, hashlib.sha256(source.encode()).hexdigest()))
                conn.execute("INSERT INTO business.cell_identity(cell_id) VALUES ('upgrade')")
                for tenant in ("first", "second"):
                    with conn.transaction():
                        conn.execute("SELECT set_config('app.tenant_id',%s,true)", (tenant,))
                        conn.execute("INSERT INTO business.tenant_bindings(tenant_id,routing_epoch) VALUES (%s,1)", (tenant,))
                        conn.execute(
                            "INSERT INTO business.monitor_rules(tenant_id,id,definition) VALUES (%s,'old','{}')", (tenant,),
                        )
                assert conn.execute("SELECT * FROM business.monitor_rules").fetchall() == []
                migrate(conn, "data", runtime, "upgrade")
                migrate(conn, "data", runtime, "upgrade")
                assert conn.execute(
                    "SELECT tenant_id FROM scheduler.work WHERE kind='realtime' ORDER BY tenant_id"
                ).fetchall() == [{"tenant_id": "first"}, {"tenant_id": "second"}]
                assert conn.execute("SELECT * FROM business.monitor_rules").fetchall() == []
                assert conn.execute(
                    "SELECT relforcerowsecurity FROM pg_class WHERE oid='business.monitor_rules'::regclass"
                ).fetchone()["relforcerowsecurity"]
        finally:
            root.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
            root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(owner)))
            root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime)))
