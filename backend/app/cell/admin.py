"""Explicit, offline administration. Runtime never receives these credentials."""

import argparse
import hashlib
import json
import os
import uuid
from getpass import getpass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.cell.passwords import hash_password, normalize_email, valid_record

SQL_ROOT = Path(__file__).with_name("sql")


def migrate(
    conn, kind: str, runtime_role: str, cell_id: str | None = None, *, target_version: int = 4,
) -> None:
    if target_version not in {4, 5, 6}:
        raise ValueError("unsupported migration target")
    schema = "control" if kind == "control" else "business"
    role = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (runtime_role,)
    ).fetchone()
    if not role or role["rolsuper"] or role["rolbypassrls"]:
        raise ValueError("runtime role must already exist without superuser/BYPASSRLS")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"tsp-migrate-{schema}",))
        exists = conn.execute("SELECT to_regclass(%s) AS relation", (f"{schema}.schema_version",)).fetchone()
        if not exists["relation"]:
            # Hash the same bytes the guard below re-reads. Decoding to text first
            # normalises CRLF to LF, which breaks the checksum on Windows checkouts.
            source = (SQL_ROOT / f"{kind}_v1.sql").read_bytes()
            conn.execute(source.decode("utf-8"))
            conn.execute(
                sql.SQL("INSERT INTO {}.schema_version VALUES (1, %s)").format(sql.Identifier(schema)),
                (hashlib.sha256(source).hexdigest(),),
            )
        versions = conn.execute(
            sql.SQL("SELECT * FROM {}.schema_version ORDER BY version").format(sql.Identifier(schema))
        ).fetchall()
        for version in versions:
            path = SQL_ROOT / f"{kind}_v{version['version']}.sql"
            if not path.exists() or version["checksum"] != hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError("schema checksum/version mismatch")
        if len(versions) > target_version:
            raise ValueError("database is newer than requested migration target")
        for number in range(len(versions) + 1, target_version + 1):
            source = (SQL_ROOT / f"{kind}_v{number}.sql").read_bytes()
            conn.execute(source.decode("utf-8"))
            conn.execute(
                sql.SQL("INSERT INTO {}.schema_version VALUES (%s, %s)").format(sql.Identifier(schema)),
                (number, hashlib.sha256(source).hexdigest()),
            )
        if kind == "data":
            if not cell_id:
                raise ValueError("cell_id required for a data database")
            conn.execute(
                "INSERT INTO business.cell_identity(cell_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (cell_id,),
            )
            identity = conn.execute("SELECT cell_id FROM business.cell_identity").fetchone()
            if identity["cell_id"] != cell_id:
                raise ValueError("database belongs to a different Cell")
        grant_runtime(conn, kind, runtime_role)
        if target_version >= 5:
            grant_full_runtime(conn, kind, runtime_role)


def grant_full_runtime(conn, kind: str, role: str) -> None:
    """Separate identity/data grants; no migration or ownership permissions."""
    schema = "control" if kind == "control" else "business"
    tables = (
        ["users", "tenants", "memberships", "sessions", "user_roles",
         "registration_config", "verification_codes", "provisioning_jobs",
         "rate_limits", "user_preferences", "full_worker_health"]
        if kind == "control" else ["documents", "research_jobs", "research_artifacts", "full_events",
                                   "full_paper_accounts", "full_paper_orders", "full_paper_nav"]
    )
    for table in tables:
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {}.{} TO {}").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(role),
            )
        )
    if kind == "data":
        conn.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE business.full_events_sequence_seq TO {}").format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION scheduler.guard(text,text,text) TO {}").format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION scheduler.renew_full(text,text,text) TO {}").format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT ON business.full_paper_fills TO {}")
            .format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON business.full_paper_fills FROM {}")
            .format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION business.initialize_tenant(text,bigint) TO {}")
            .format(sql.Identifier(role))
        )


def bootstrap_full_identity(control) -> bool:
    """Offline, idempotent administrator initialization. Never reset a password."""
    salt, digest = hash_password("Admin123")
    with control.transaction():
        control.execute("SELECT pg_advisory_xact_lock(hashtext('tsp-system-admin-bootstrap'))")
        existing = control.execute(
            "SELECT id FROM control.users WHERE username='admin' FOR UPDATE"
        ).fetchone()
        if existing:
            role = control.execute(
                "SELECT 1 FROM control.user_roles WHERE user_id=%s AND role_id='admin'",
                (existing["id"],),
            ).fetchone()
            if not role:
                raise ValueError("admin identifier collision; explicit remediation required")
            return False
        user_id = uuid.uuid4().hex
        control.execute(
            """INSERT INTO control.users(id,username,password_salt,password_hash)
               VALUES(%s,'admin',%s,%s)""", (user_id, salt, digest),
        )
        control.execute(
            "INSERT INTO control.user_roles(user_id,role_id) VALUES(%s,'admin')", (user_id,),
        )
    return True


def grant_runtime(conn, kind: str, role: str) -> None:
    schema = "control" if kind == "control" else "business"
    conn.execute(
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema))
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role))
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier(role)
        )
    )
    tables = (
        ["sessions", "api_tokens", "memberships", "login_attempts"] if kind == "control"
        else ["watchlist_items", "watchlist_groups", "watchlist_group_items", "strategies",
              "strategy_jobs", "monitor_rules", "monitor_state", "alerts", "paper_accounts", "paper_orders",
              "notification_channels", "notification_outbox", "trade_automations", "trade_intents"]
    )
    for table in tables:
        conn.execute(
            sql.SQL("GRANT INSERT, UPDATE, DELETE ON {}.{} TO {}").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(role)
            )
        )
    conn.execute(
        sql.SQL("GRANT INSERT ON {}.audit TO {}").format(sql.Identifier(schema), sql.Identifier(role))
    )
    if kind == "control":
        # PostgreSQL row locking requires UPDATE on at least one column.
        for table in ("users", "cells"):
            conn.execute(
                sql.SQL("GRANT UPDATE (id) ON control.{} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
        conn.execute(
            sql.SQL("GRANT UPDATE (policy_version) ON control.tenants TO {}").format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE control.audit_id_seq TO {}").format(sql.Identifier(role))
        )
    else:
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA scheduler TO {}").format(sql.Identifier(role)))
        for function in ("claim(text,integer,integer,text)", "finish(text,text,text,bigint,timestamptz)"):
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION scheduler." + function + " TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("GRANT INSERT ON business.paper_fills TO {}").format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT UPDATE (tenant_id) ON business.tenant_bindings TO {}").format(
                sql.Identifier(role)
            )
        )


def add_user(conn, email: str, password: str) -> str:
    user_id = uuid.uuid4().hex
    salt, digest = hash_password(password)
    with conn.transaction():
        conn.execute(
            "INSERT INTO control.users(id, email, password_salt, password_hash) VALUES (%s, %s, %s, %s)",
            (user_id, normalize_email(email), salt, digest),
        )
        if conn.execute("SELECT to_regclass('control.user_roles') AS relation").fetchone()["relation"]:
            conn.execute("INSERT INTO control.user_roles VALUES(%s,'tenant')", (user_id,))
    return user_id


def register_cell(conn, cell_id: str, api_url: str) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(api_url)
    if not parsed.hostname or parsed.username or parsed.password or (
        parsed.scheme != "https"
        and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})
    ):
        raise ValueError("Cell URL requires HTTPS (except loopback development)")
    with conn.transaction():
        conn.execute(
            """INSERT INTO control.cells(id, api_url) VALUES (%s, %s)
               ON CONFLICT (id) DO UPDATE SET api_url = excluded.api_url""",
            (cell_id, api_url.rstrip("/")),
        )


def provision_tenant(control, data, tenant_id: str, name: str, cell_id: str, owner_id: str, realtime_units: int = 0) -> None:
    """Persist stable placement; leave failed provisioning in moving state for retry."""
    with control.transaction():
        control.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"tsp-tenant-{tenant_id}",))
        cell = control.execute(
            "SELECT tenant_limit,realtime_limit FROM control.cells WHERE id = %s AND status = 'active' FOR UPDATE", (cell_id,)
        ).fetchone()
        if not cell:
            raise ValueError("active Cell must be registered first")
        tenant = control.execute("SELECT * FROM control.tenants WHERE id = %s FOR UPDATE", (tenant_id,)).fetchone()
        if tenant:
            owner = control.execute(
                "SELECT role FROM control.memberships WHERE tenant_id = %s AND user_id = %s",
                (tenant_id, owner_id),
            ).fetchone()
            if tenant["cell_id"] != cell_id or tenant["routing_epoch"] != 1 or not owner or owner["role"] != "owner":
                raise ValueError("existing tenant placement/owner differs; use a migration workflow")
            if tenant["status"] == "suspended":
                raise ValueError("provision cannot reactivate a suspended tenant")
        else:
            count = control.execute(
                "SELECT count(*) AS n FROM control.tenants WHERE cell_id=%s", (cell_id,)
            ).fetchone()["n"]
            if count >= cell["tenant_limit"]:
                raise ValueError("Cell capacity exhausted; provision in another Cell")
            used = control.execute(
                "SELECT coalesce(sum(realtime_reservation),0) AS n FROM control.tenants WHERE cell_id=%s", (cell_id,)
            ).fetchone()["n"]
            if realtime_units < 0 or used + realtime_units > cell["realtime_limit"]:
                raise ValueError("Cell realtime reservation exhausted")
            control.execute(
                """INSERT INTO control.tenants(id, name, cell_id, status,realtime_reservation)
                   VALUES (%s, %s, %s, 'moving',%s)""", (tenant_id, name, cell_id, realtime_units),
            )
            control.execute(
                "INSERT INTO control.memberships VALUES (%s, %s, 'owner')", (tenant_id, owner_id)
            )
    with data.transaction():
        data.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
        identity = data.execute("SELECT cell_id FROM business.cell_identity").fetchone()
        if not identity or identity["cell_id"] != cell_id:
            raise ValueError("wrong Cell data database; tenant remains unavailable")
        data.execute(
            """INSERT INTO business.tenant_bindings(tenant_id, routing_epoch) VALUES (%s, 1)
               ON CONFLICT DO NOTHING""", (tenant_id,),
        )
        binding = data.execute(
            "SELECT routing_epoch, enabled FROM business.tenant_bindings WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        if not binding["enabled"] or binding["routing_epoch"] != 1:
            raise ValueError("binding cannot be changed by provisioning")
    with control.transaction():
        control.execute(
            """UPDATE control.tenants SET status = 'active'
               WHERE id = %s AND cell_id = %s AND routing_epoch = 1 AND status = 'moving'""",
            (tenant_id, cell_id),
        )


def set_tenant_status(control, tenant_id: str, status: str, actor_id: str) -> None:
    if status not in {"active", "suspended"}:
        raise ValueError("unsupported tenant status")
    with control.transaction():
        tenant = control.execute(
            "SELECT status FROM control.tenants WHERE id = %s FOR UPDATE", (tenant_id,)
        ).fetchone()
        if not tenant or tenant["status"] == "moving":
            raise ValueError("missing tenant or provisioning/migration incomplete")
        control.execute(
            "UPDATE control.tenants SET status = %s, policy_version = policy_version + 1 WHERE id = %s",
            (status, tenant_id),
        )
        control.execute(
            """INSERT INTO control.audit(tenant_id, actor_id, action, target_id, request_id)
               VALUES (%s, %s, %s, %s, %s)""",
            (tenant_id, actor_id, "tenant." + status, tenant_id, uuid.uuid4().hex),
        )


def import_users(control, directory: Path, owner_id: str) -> dict:
    auth = json.loads((directory / "auth.json").read_text(encoding="utf-8"))
    users = auth.get("users")
    if not isinstance(users, dict) or owner_id not in users:
        raise ValueError("explicit owner must be an existing email account; claim password-only auth first")
    with control.transaction():
        for user in users.values():
            if not valid_record(user.get("password_salt"), user.get("password_hash")):
                raise ValueError("invalid legacy password record")
            email = normalize_email(user["email"])
            existing = control.execute(
                "SELECT * FROM control.users WHERE id = %s OR email = %s", (user["id"], email)
            ).fetchall()
            if existing:
                if len(existing) != 1 or any(
                    existing[0][key] != user[key] for key in ("id", "password_salt", "password_hash")
                ) or existing[0]["email"] != email:
                    raise ValueError("identity collision; import aborted")
            else:
                control.execute(
                    "INSERT INTO control.users(id, email, password_salt, password_hash) VALUES (%s, %s, %s, %s)",
                    (user["id"], email, user["password_salt"], user["password_hash"]),
                )
            if control.execute("SELECT to_regclass('control.user_roles') AS relation").fetchone()["relation"]:
                control.execute(
                    "INSERT INTO control.user_roles VALUES(%s,'tenant') ON CONFLICT DO NOTHING", (user["id"],),
                )
    return users


def import_full_accounts(control, directory: Path, tenant_id: str, owner_id: str) -> dict:
    """Bind explicitly imported legacy accounts; never infer ownership by email."""
    from app.multitenancy.preferences import DEFAULTS

    auth = json.loads((directory / "auth.json").read_text(encoding="utf-8"))
    preferences_path = directory / "preferences.json"
    display = json.loads(preferences_path.read_text(encoding="utf-8")) if preferences_path.exists() else {}
    display = {key: display[key] for key in DEFAULTS if key in display}
    salt, digest = auth.get("registration_secret_salt"), auth.get("registration_secret_hash")
    if (salt or digest) and not valid_record(salt, digest):
        raise ValueError("invalid legacy registration-secret record")
    with control.transaction():
        tenant = control.execute(
            "SELECT id FROM control.tenants WHERE id=%s AND status='suspended' FOR UPDATE", (tenant_id,),
        ).fetchone()
        if not tenant:
            raise ValueError("full account import requires an explicitly suspended legacy tenant")
        users = import_users(control, directory, owner_id)
        for record in users.values():
            existing = control.execute(
                "SELECT * FROM control.users WHERE id=%s FOR UPDATE", (record["id"],),
            ).fetchone()
            if existing["username"] or existing["default_tenant_id"] not in {None, tenant_id}:
                raise ValueError("existing account identity/default tenant differs")
            roles = control.execute(
                "SELECT role_id FROM control.user_roles WHERE user_id=%s", (record["id"],),
            ).fetchall()
            if {row["role_id"] for row in roles} != {"tenant"}:
                raise ValueError("legacy accounts must retain tenant system role")
            control.execute(
                """INSERT INTO control.memberships VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                (tenant_id, record["id"], "owner" if record["id"] == owner_id else "member"),
            )
            control.execute(
                """UPDATE control.users SET default_tenant_id=%s,
                   created_at=coalesce(to_timestamp(%s),created_at),auth_version=auth_version+1 WHERE id=%s""",
                (tenant_id, record.get("created_at"), record["id"]),
            )
            control.execute("DELETE FROM control.sessions WHERE user_id=%s", (record["id"],))
            control.execute(
                "UPDATE control.api_tokens SET revoked_at=now() WHERE user_id=%s", (record["id"],),
            )
            if display:
                control.execute(
                    """INSERT INTO control.user_preferences(user_id,value) VALUES(%s,%s)
                       ON CONFLICT DO NOTHING""", (record["id"], Jsonb(display)),
                )
        if salt and digest:
            existing = control.execute("SELECT * FROM control.registration_config").fetchone()
            if existing and (existing["secret_salt"] != salt or existing["secret_hash"] != digest):
                raise ValueError("registration secret differs; import must not overwrite it")
            control.execute(
                """INSERT INTO control.registration_config(secret_salt,secret_hash)
                   VALUES(%s,%s) ON CONFLICT DO NOTHING""", (salt, digest),
            )
    return {"users": len(users), "registration_secret_imported": bool(salt), "preferences": len(display)}


def import_legacy(control, data, directory: Path, tenant_id: str, owner_id: str) -> dict:
    """Run against a consistent offline copy, after provisioning a chosen tenant."""
    groups_path = directory / "watchlist_groups.json"
    groups = json.loads(groups_path.read_text(encoding="utf-8")) if groups_path.exists() else []
    if not isinstance(groups, list):
        raise ValueError("invalid legacy groups")
    alerts = []
    alerts_path = directory / "alerts.jsonl"
    if alerts_path.exists():
        for line in alerts_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            timestamp = event.get("ts") if isinstance(event, dict) else None
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or timestamp <= 0
            ):
                raise ValueError("invalid legacy alert")
            alerts.append(event)
    rows = []
    watchlist_path = directory / "watchlist.parquet"
    if watchlist_path.exists():
        import polars as pl

        rows = pl.read_parquet(watchlist_path).to_dicts()
    # No session, global API token, secret or private strategy assignment is inferred.
    with control.transaction():
        tenant = control.execute("SELECT * FROM control.tenants WHERE id = %s FOR UPDATE", (tenant_id,)).fetchone()
        owner = control.execute(
            "SELECT role FROM control.memberships WHERE tenant_id = %s AND user_id = %s",
            (tenant_id, owner_id),
        ).fetchone()
        if not tenant or tenant["status"] != "suspended" or not owner or owner["role"] != "owner":
            raise ValueError("import requires a suspended tenant with the explicitly chosen owner")
        users = import_users(control, directory, owner_id)
        with data.transaction():
            cell = data.execute("SELECT cell_id FROM business.cell_identity").fetchone()
            if cell["cell_id"] != tenant["cell_id"]:
                raise ValueError("wrong Cell for import")
            data.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            binding = data.execute(
                "SELECT routing_epoch FROM business.tenant_bindings WHERE tenant_id = %s FOR UPDATE", (tenant_id,)
            ).fetchone()
            if not binding or binding["routing_epoch"] != tenant["routing_epoch"]:
                raise ValueError("missing/stale tenant binding")
            # Admin roles can bypass RLS; always filter explicitly in migration code.
            if data.execute(
                "SELECT 1 FROM business.watchlist_items WHERE tenant_id = %s LIMIT 1", (tenant_id,)
            ).fetchone() or data.execute(
                "SELECT 1 FROM business.watchlist_groups WHERE tenant_id = %s LIMIT 1", (tenant_id,)
            ).fetchone() or data.execute(
                "SELECT 1 FROM business.alerts WHERE tenant_id = %s LIMIT 1", (tenant_id,)
            ).fetchone():
                raise ValueError("destination must be empty; import never overwrites")
            for position, group in enumerate(groups):
                data.execute(
                    "INSERT INTO business.watchlist_groups(tenant_id, id, name, color, position) VALUES (%s, %s, %s, %s, %s)",
                    (tenant_id, group["id"], group["name"], group.get("color", "sky"), position),
                )
            for row in rows:
                data.execute(
                    """INSERT INTO business.watchlist_items(tenant_id, symbol, note, added_at)
                       VALUES (%s, %s, %s, coalesce(%s::timestamptz, now()))""",
                    (tenant_id, row["symbol"], row.get("note") or "", row.get("added_at") or None),
                )
                group_ids = row.get("group_ids") or ([row["group_id"]] if row.get("group_id") else [])
                for group_id in dict.fromkeys(group_ids):
                    data.execute(
                        "INSERT INTO business.watchlist_group_items VALUES (%s, %s, %s)",
                        (tenant_id, group_id, row["symbol"]),
                    )
            for event in alerts:
                encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                data.execute(
                    """INSERT INTO business.alerts
                       (tenant_id,id,dedupe_key,event,created_at)
                       VALUES (%s,%s,%s,%s,to_timestamp(%s / 1000.0))""",
                    (tenant_id, digest[:32], "legacy:" + digest, Jsonb(event), event["ts"]),
                )
    return {"users": len(users), "groups": len(groups), "items": len(rows), "alerts": len(alerts)}


def connect_admin(variable: str):
    value = os.environ.get(variable)
    if not value:
        raise ValueError(f"{variable} required")
    return psycopg.connect(value, autocommit=True, row_factory=dict_row)

def sync_catalog(control) -> int:
    from app.cell.compute import catalog

    definitions = catalog()
    with control.transaction():
        for row in definitions:
            control.execute(
                """INSERT INTO control.strategy_catalog(id,definition,code_hash) VALUES (%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,code_hash=excluded.code_hash,updated_at=now()""",
                (row["id"], Jsonb(row["definition"]), row["code_hash"]),
            )
    return len(definitions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync-strategies")
    commands.add_parser("bootstrap-full-identity")
    choose = commands.add_parser("select-cell")
    choose.add_argument("--realtime-units", type=int, default=1)
    capacity = commands.add_parser("cell-capacity")
    capacity.add_argument("--cell-id", required=True)
    capacity.add_argument("--max-tenants", type=int, default=1000)
    capacity.add_argument("--realtime-units", type=int, default=200)
    capacity.add_argument("--max-tenant-units", type=int, default=100)
    plan = commands.add_parser("split-plan")
    plan.add_argument("--input", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    loads = commands.add_parser("cell-load")
    loads.add_argument("--output", type=Path, required=True)
    sync_load = commands.add_parser("sync-cell-load")
    sync_load.add_argument("--cell-id", required=True)
    migration = commands.add_parser("migrate")
    migration.add_argument("--kind", choices=["control", "data"], required=True)
    migration.add_argument("--runtime-role", required=True)
    migration.add_argument("--cell-id")
    migration.add_argument("--target-version", type=int, choices=[4, 5, 6], default=4)
    cell = commands.add_parser("register-cell")
    cell.add_argument("--cell-id", required=True)
    cell.add_argument("--api-url", required=True)
    user = commands.add_parser("create-user")
    user.add_argument("--email", required=True)
    tenant = commands.add_parser("provision-tenant")
    tenant.add_argument("--tenant-id", required=True)
    tenant.add_argument("--name", required=True)
    tenant.add_argument("--cell-id", required=True)
    tenant.add_argument("--owner-id", required=True)
    tenant.add_argument("--realtime-units", type=int, default=0)
    importer = commands.add_parser("import-legacy")
    importer.add_argument("--directory", type=Path, required=True)
    importer.add_argument("--tenant-id", required=True)
    importer.add_argument("--owner-id", required=True)
    identities = commands.add_parser("import-users")
    identities.add_argument("--directory", type=Path, required=True)
    identities.add_argument("--owner-id", required=True)
    full_identities = commands.add_parser("import-full-accounts")
    full_identities.add_argument("--directory", type=Path, required=True)
    full_identities.add_argument("--owner-id", required=True)
    full_identities.add_argument("--tenant-id", required=True)
    full_product = commands.add_parser("import-full-product")
    full_product.add_argument("--directory", type=Path, required=True)
    full_product.add_argument("--owner-id", required=True)
    full_product.add_argument("--tenant-id", required=True)
    full_product.add_argument("--report", type=Path, required=True)
    full_product.add_argument("--channel-map", default="{}", help="Explicit legacy name to tenant channel ID JSON")
    verification = commands.add_parser("verify-full-import")
    verification.add_argument("--directory", type=Path, required=True)
    verification.add_argument("--tenant-id", required=True)
    verification.add_argument("--report", type=Path, required=True)
    status = commands.add_parser("tenant-status")
    status.add_argument("--tenant-id", required=True)
    status.add_argument("--status", choices=["active", "suspended"], required=True)
    status.add_argument("--actor-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "migrate":
            variable = "TSP_CONTROL_ADMIN_DSN" if args.kind == "control" else "TSP_DATA_ADMIN_DSN"
            with connect_admin(variable) as conn:
                migrate(
                    conn, args.kind, args.runtime_role, args.cell_id,
                    target_version=args.target_version,
                )
        else:
            with connect_admin("TSP_CONTROL_ADMIN_DSN") as control:
                if args.command == "bootstrap-full-identity":
                    print(json.dumps({"created": bootstrap_full_identity(control)}))
                elif args.command == "create-user":
                    password = os.environ.get("TSP_BOOTSTRAP_PASSWORD") or getpass("Password: ")
                    print(json.dumps({"user_id": add_user(control, args.email, password)}))
                elif args.command == "register-cell":
                    register_cell(control, args.cell_id, args.api_url)
                elif args.command == "import-users":
                    users = import_users(control, args.directory, args.owner_id)
                    print(json.dumps({"users": len(users)}))
                elif args.command == "import-full-accounts":
                    print(json.dumps(import_full_accounts(
                        control, args.directory, args.tenant_id, args.owner_id,
                    )))
                elif args.command in {"import-full-product", "verify-full-import"}:
                    from app.multitenancy.executor import ComputeExecutor
                    from app.multitenancy.legacy_import import import_full_product, verify_import

                    with connect_admin("TSP_DATA_ADMIN_DSN") as data:
                        if args.command == "import-full-product":
                            result = import_full_product(
                                control, data, args.directory, args.tenant_id, args.owner_id,
                                executor=ComputeExecutor(), channel_map=json.loads(args.channel_map),
                            )
                        else:
                            result = verify_import(data, args.directory, args.tenant_id)
                        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                        print(json.dumps(result))
                elif args.command == "tenant-status":
                    set_tenant_status(control, args.tenant_id, args.status, args.actor_id)
                elif args.command == "sync-strategies":
                    print(json.dumps({"strategies": sync_catalog(control)}))
                elif args.command == "select-cell":
                    from app.cell.placement import cell_loads, select_cell
                    print(json.dumps({"cell_id": select_cell(cell_loads(control), args.realtime_units)}))
                elif args.command == "split-plan":
                    from app.cell.placement import planning_loads, split_plan
                    tenants = json.loads(args.input.read_text())
                    result = split_plan(tenants, planning_loads(control, tenants))
                    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
                    print(json.dumps({"tenants": len(result["assignments"]), "output": str(args.output)}))
                elif args.command == "cell-load":
                    with connect_admin("TSP_DATA_ADMIN_DSN") as data:
                        result = data.execute(
                            "SELECT tenant_id,units AS realtime_units FROM scheduler.tenant_load ORDER BY units DESC,tenant_id"
                        ).fetchall()
                        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
                        print(json.dumps({"tenants": len(result), "output": str(args.output)}))
                elif args.command == "sync-cell-load":
                    with connect_admin("TSP_DATA_ADMIN_DSN") as data:
                        if data.execute("SELECT cell_id FROM business.cell_identity").fetchone()["cell_id"] != args.cell_id:
                            raise ValueError("wrong Cell database")
                        snapshot = data.execute("SELECT tenant_id,units FROM scheduler.tenant_load").fetchall()
                        with control.transaction():
                            control.execute("SELECT id FROM control.cells WHERE id=%s FOR UPDATE", (args.cell_id,))
                            for row in snapshot:
                                control.execute(
                                    "UPDATE control.tenants SET realtime_reservation=%s WHERE id=%s AND cell_id=%s",
                                    (row["units"], row["tenant_id"], args.cell_id),
                                )
                        print(json.dumps({"tenants": len(snapshot)}))
                elif args.command == "cell-capacity":
                    if min(args.max_tenants, args.realtime_units, args.max_tenant_units) < 1:
                        raise ValueError("capacity must be positive")
                    with connect_admin("TSP_DATA_ADMIN_DSN") as data:
                        if data.execute("SELECT cell_id FROM business.cell_identity").fetchone()["cell_id"] != args.cell_id:
                            raise ValueError("wrong Cell database")
                        # Independent commits; use while provisioning is paused.
                        with data.transaction():
                            data.execute(
                                "UPDATE scheduler.capacity SET max_tenants=%s,max_realtime_units=%s,max_tenant_units=%s",
                                (args.max_tenants, args.realtime_units, args.max_tenant_units),
                            )
                        with control.transaction():
                            if not control.execute(
                                "UPDATE control.cells SET tenant_limit=%s,realtime_limit=%s WHERE id=%s",
                                (args.max_tenants, args.realtime_units, args.cell_id),
                            ).rowcount:
                                raise ValueError("Cell not found")
                else:
                    with connect_admin("TSP_DATA_ADMIN_DSN") as data:
                        if args.command == "provision-tenant":
                            provision_tenant(control, data, args.tenant_id, args.name, args.cell_id, args.owner_id, args.realtime_units)
                        else:
                            print(json.dumps(import_legacy(control, data, args.directory, args.tenant_id, args.owner_id)))
    except (ValueError, TypeError, KeyError, OSError, psycopg.Error) as exc:
        # Database exceptions may contain hashes/DSNs/row data. Print only a safe category.
        parser.exit(1, f"Cell administration failed ({type(exc).__name__}); check inputs and database state.\n")


if __name__ == "__main__":
    main()
