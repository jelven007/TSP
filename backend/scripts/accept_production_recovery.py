"""Exercise joint recovery without touching a production service or dataset.

The rehearsal creates one disposable PostgreSQL container, a source Cell, and
an independently restored Cell. It restores control/data dumps together with
public releases, platform configuration, and private artifacts, then verifies
identity, RLS, events, paper trading, research artifacts, cutover, and a
rollback that preserves a post-cutover business increment.

Run from backend:
    uv run --frozen --no-sync python -m scripts.accept_production_recovery
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from datetime import time as wall_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from app.cell.admin import migrate
from app.cell.config import CellSettings
from app.cell.database import Databases
from app.market_time import CN_TZ
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.events import EventLog, append_event
from app.multitenancy.identity import AccountIdentity
from app.multitenancy.paper_storage import TenantPaper
from app.multitenancy.research import ResearchJobs
from app.strategy import paper
from scripts.accept_external_sse import (
    _docker,
    _image_evidence,
    _published_port,
    _remove_container,
    _wait_postgres,
)
from scripts.accept_phase2_step1 import _cluster, _login, _seed

DEFAULT_POSTGRES_IMAGE = "postgres:16"
POSTGRES_PASSWORD = "recovery-disposable-postgres"
RESTORE_PASSWORD = "recovery-runtime-password"
DAY = date(2026, 10, 16)
SYMBOL = "600000.SH"

CONTROL_TABLES = (
    ("control", "schema_version"),
    ("control", "users"),
    ("control", "cells"),
    ("control", "tenants"),
    ("control", "memberships"),
    ("control", "sessions"),
    ("control", "user_roles"),
    ("control", "user_preferences"),
    ("control", "full_worker_health"),
)
DATA_TABLES = (
    ("business", "schema_version"),
    ("business", "tenant_bindings"),
    ("business", "documents"),
    ("business", "full_paper_accounts"),
    ("business", "full_paper_orders"),
    ("business", "full_paper_fills"),
    ("business", "full_paper_nav"),
    ("business", "research_jobs"),
    ("business", "research_artifacts"),
    ("business", "full_events"),
    ("scheduler", "work"),
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _tree_inventory(root: Path) -> dict:
    entries: list[dict] = []
    total = 0
    if not root.exists():
        raise FileNotFoundError(root)
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            entries.append({"path": relative, "type": "symlink", "target": target})
        elif path.is_file():
            content = path.read_bytes()
            total += len(content)
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": len(content),
                    "sha256": _sha256(content),
                }
            )
        elif path.is_dir():
            entries.append({"path": relative, "type": "directory"})
    return {
        "entries": len(entries),
        "files": sum(item["type"] == "file" for item in entries),
        "bytes": total,
        "sha256": _sha256(_canonical(entries).encode()),
    }


def _copy_tree(source: Path, target: Path) -> dict:
    if target.exists():
        raise FileExistsError(target)
    shutil.copytree(source, target, symlinks=True)
    source_inventory = _tree_inventory(source)
    target_inventory = _tree_inventory(target)
    if source_inventory != target_inventory:
        raise AssertionError(f"directory copy mismatch: {source} -> {target}")
    return target_inventory


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _set_current(root: Path, release_id: str) -> None:
    target = Path("releases") / release_id
    temporary = root / (".current-" + uuid.uuid4().hex)
    temporary.symlink_to(target, target_is_directory=True)
    current = root / "current"
    os.replace(temporary, current)


def _seed_files(root: Path) -> dict[str, Path]:
    platform = root / "platform-config"
    public = root / "public"
    private = root / "private-artifacts"
    edge = root / "edge"

    _write_json(
        platform / "preferences.json",
        {"realtime_data_provider": "rustdx", "generation": 1},
    )
    _write_json(
        platform / "secrets.json",
        {"fixture_only": "not-a-production-secret"},
    )
    (platform / "data_sources").mkdir(parents=True)
    (platform / "data_sources" / "recovery.yaml").write_text(
        "name: recovery_fixture\n",
        encoding="utf-8",
    )

    release = public / "releases" / "release-001"
    daily = release / "kline_daily" / f"date={DAY.isoformat()}"
    daily.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": [SYMBOL, SYMBOL],
            "date": [DAY - timedelta(days=1), DAY],
            "open": [9.8, 10.0],
            "high": [10.1, 10.5],
            "low": [9.7, 9.9],
            "close": [10.0, 10.4],
            "volume": [1_000_000.0, 1_200_000.0],
            "amount": [10_000_000.0, 12_300_000.0],
        }
    ).write_parquet(daily / "part.parquet")
    _write_json(
        release / ".public_release.json",
        {"release_id": "release-001", "generation": 1},
    )
    _set_current(public, "release-001")

    (private / "tenant-a").mkdir(parents=True)
    (private / "tenant-a" / "legacy-export.bin").write_bytes(
        b"private-artifact-before-final-backup"
    )
    _write_json(edge / "route.json", {"active": "source", "generation": 1})
    return {
        "platform": platform,
        "public": public,
        "private": private,
        "edge": edge,
    }


def _final_file_increment(paths: dict[str, Path]) -> None:
    platform = paths["platform"]
    preferences = json.loads((platform / "preferences.json").read_text(encoding="utf-8"))
    preferences["generation"] = 2
    preferences["minute_refresh_enabled"] = True
    _write_json(platform / "preferences.json", preferences)

    public = paths["public"]
    source = public / "releases" / "release-001"
    target = public / "releases" / "release-002"
    shutil.copytree(source, target)
    _write_json(
        target / ".public_release.json",
        {"release_id": "release-002", "generation": 2},
    )
    (target / "recovery-marker.txt").write_text(
        "included in final backup\n",
        encoding="utf-8",
    )
    _set_current(public, "release-002")
    (paths["private"] / "tenant-a" / "final-export.bin").write_bytes(
        b"private-artifact-in-final-backup"
    )


def _database_inventory(control, data) -> dict:
    inventories: dict[str, dict] = {}
    for connection, tables in ((control, CONTROL_TABLES), (data, DATA_TABLES)):
        for schema, table in tables:
            rows = connection.execute(
                sql.SQL("SELECT to_jsonb(t) AS value FROM {}.{} AS t").format(
                    sql.Identifier(schema),
                    sql.Identifier(table),
                )
            ).fetchall()
            encoded = sorted(_canonical(row["value"]) for row in rows)
            inventories[f"{schema}.{table}"] = {
                "rows": len(encoded),
                "sha256": _sha256("\n".join(encoded).encode()),
            }
    sequence = data.execute(
        "SELECT last_value,is_called FROM business.full_events_sequence_seq"
    ).fetchone()
    inventories["business.full_events_sequence_seq"] = {
        "rows": 1,
        "sha256": _sha256(_canonical(sequence).encode()),
    }
    return {
        "tables": inventories,
        "rows": sum(value["rows"] for value in inventories.values()),
        "sha256": _sha256(_canonical(inventories).encode()),
    }


def _dump_database(container: str, database: str) -> bytes:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--username",
            "postgres",
            "--dbname",
            database,
        ],
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"pg_dump failed for {database}: "
            + result.stderr.decode(errors="replace")[-4000:]
        )
    if not result.stdout.startswith(b"PGDMP"):
        raise RuntimeError(f"invalid PostgreSQL dump for {database}")
    return result.stdout


def _restore_database(container: str, database: str, payload: bytes) -> None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--username",
            "postgres",
            "--dbname",
            database,
        ],
        input=payload,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"pg_restore failed for {database}: "
            + result.stderr.decode(errors="replace")[-4000:]
        )


def _backup(
    postgres: str,
    cluster,
    paths: dict[str, Path],
    target: Path,
) -> dict:
    started = time.perf_counter()
    control_dump = _dump_database(postgres, cluster.dbnames[0])
    data_dump = _dump_database(postgres, cluster.dbnames[1])
    files = {}
    for name in ("platform", "public", "private", "edge"):
        files[name] = _copy_tree(paths[name], target / name)
    elapsed = time.perf_counter() - started
    return {
        "control_dump": control_dump,
        "data_dump": data_dump,
        "evidence": {
            "elapsed_seconds": round(elapsed, 3),
            "control_dump": {
                "bytes": len(control_dump),
                "sha256": _sha256(control_dump),
            },
            "data_dump": {
                "bytes": len(data_dump),
                "sha256": _sha256(data_dump),
            },
            "directories": files,
        },
    }


def _verify_backup_integrity(backup: dict, backup_root: Path) -> dict:
    evidence = backup["evidence"]
    dumps = {}
    for name in ("control", "data"):
        payload = backup[f"{name}_dump"]
        actual = {
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
        if actual != evidence[f"{name}_dump"]:
            raise AssertionError(f"{name} database dump integrity mismatch")
        dumps[name] = actual

    directories = {}
    for name, expected in evidence["directories"].items():
        actual = _tree_inventory(backup_root / name)
        if actual != expected:
            raise AssertionError(f"{name} backup directory integrity mismatch")
        directories[name] = actual
    return {
        "verified": True,
        "dumps": dumps,
        "directories": directories,
    }


def _exercise_corruption_guards(
    *,
    root_dsn: str,
    postgres: str,
    backup: dict,
    backup_root: Path,
    scratch: Path,
    traffic: Path,
) -> dict:
    corrupted_dump = {
        **backup,
        "control_dump": backup["control_dump"][:-64],
    }
    try:
        _verify_backup_integrity(corrupted_dump, backup_root)
    except AssertionError as exc:
        dump_preflight = str(exc)
    else:
        raise AssertionError("truncated database dump passed integrity preflight")

    tampered_root = scratch / "tampered-backup"
    shutil.copytree(backup_root, tampered_root, symlinks=True)
    tampered_file = tampered_root / "public" / "current" / "recovery-marker.txt"
    tampered_file.write_bytes(tampered_file.read_bytes() + b"tampered\n")
    try:
        _verify_backup_integrity(backup, tampered_root)
    except AssertionError as exc:
        directory_preflight = str(exc)
    else:
        raise AssertionError("tampered backup directory passed integrity preflight")

    try:
        with _restored_cluster(
            root_dsn,
            postgres,
            backup["control_dump"],
            backup["data_dump"][: len(backup["data_dump"]) // 2],
        ):
            raise AssertionError("truncated dump unexpectedly restored")
    except RuntimeError as exc:
        restore_failure = str(exc).splitlines()[0]
    else:
        raise AssertionError("pg_restore accepted a truncated database dump")

    active = json.loads(traffic.read_text(encoding="utf-8"))["active"]
    if active != "source":
        raise AssertionError("failed restore changed the active traffic target")
    return {
        "truncated_dump_preflight_blocked": True,
        "truncated_dump_preflight_error": dump_preflight,
        "tampered_directory_preflight_blocked": True,
        "tampered_directory_preflight_error": directory_preflight,
        "pg_restore_truncated_dump_blocked": True,
        "pg_restore_error": restore_failure,
        "active_after_failures": active,
    }


@contextmanager
def _restored_cluster(
    root_dsn: str,
    postgres: str,
    control_dump: bytes,
    data_dump: bytes,
):
    prefix = "tsp_restore_" + uuid.uuid4().hex[:10]
    databases = [prefix + "_control", prefix + "_data"]
    roles = [prefix + "_identity", prefix + "_business"]
    with psycopg.connect(root_dsn, autocommit=True, row_factory=dict_row) as root:
        try:
            for role in roles:
                root.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.Literal(RESTORE_PASSWORD))
                )
            for database in databases:
                root.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
                )

            _restore_database(postgres, databases[0], control_dump)
            _restore_database(postgres, databases[1], data_dump)

            with ExitStack() as resources:
                admins = [
                    resources.enter_context(
                        psycopg.connect(
                            make_conninfo(root_dsn, dbname=database),
                            autocommit=True,
                            row_factory=dict_row,
                        )
                    )
                    for database in databases
                ]
                migrate(admins[0], "control", roles[0], "full", target_version=6)
                migrate(admins[1], "data", roles[1], "full", target_version=6)
                settings = CellSettings(
                    id="full",
                    schema_version=6,
                    pool_size=8,
                    control_dsn=make_conninfo(
                        root_dsn,
                        dbname=databases[0],
                        user=roles[0],
                        password=RESTORE_PASSWORD,
                    ),
                    data_dsn=make_conninfo(
                        root_dsn,
                        dbname=databases[1],
                        user=roles[1],
                        password=RESTORE_PASSWORD,
                    ),
                    stream_ticket_secret=SecretStr(
                        "recovery-restored-" + "s" * 32
                    ),
                )
                runtime = Databases(settings)
                runtime.open()
                resources.callback(runtime.close)
                yield SimpleNamespace(
                    control=admins[0],
                    data=admins[1],
                    db=runtime,
                    identity=AccountIdentity(runtime),
                    settings=settings,
                    dbnames=tuple(databases),
                )
        finally:
            for database in reversed(databases):
                root.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )
            for role in reversed(roles):
                root.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(
                        sql.Identifier(role)
                    )
                )


def _context(cluster, principal: dict, request_id: str):
    return cluster.identity.context(
        principal["account"],
        principal["tenant_id"],
        request_id,
    )


def _seed_business(cluster, principals: list[dict], public: Path) -> dict:
    contexts = [
        _context(cluster, principal, f"recovery-source-{index}")
        for index, principal in enumerate(principals)
    ]
    documents = [TenantDocuments(cluster.db, context) for context in contexts]
    documents[0].put(
        "recovery",
        "state",
        {"tenant": "alpha", "phase": "initial", "amount": 100_000},
    )
    documents[1].put(
        "recovery",
        "state",
        {"tenant": "beta", "phase": "initial", "amount": 200_000},
    )
    cluster.control.execute(
        """INSERT INTO control.user_preferences(user_id,value)
           VALUES(%s,%s) ON CONFLICT(user_id) DO UPDATE SET value=excluded.value""",
        (principals[0]["user_id"], Jsonb({"theme": "dark", "columns": ["close"]})),
    )

    with cluster.db.business_transaction(contexts[0], write=True) as conn:
        first_event = append_event(
            conn,
            contexts[0].tenant_id,
            "strategy_results_updated",
            {"phase": "initial"},
        )

    jobs = ResearchJobs(documents[0])
    job = jobs.submit(
        {
            "kind": "backtest",
            "tenant_id": contexts[0].tenant_id,
            "config": {"period": DAY.isoformat()},
            "sources": [],
            "overrides": {},
            "factors": [],
            "signals": [],
        },
        key="recovery-research",
    )
    artifact = b"recovery-research-artifact-v1"
    with cluster.db.business_transaction(contexts[0], write=True) as conn:
        conn.execute(
            """UPDATE business.research_jobs
               SET state='succeeded',result=%s,updated_at=now() WHERE id=%s""",
            (Jsonb({"stats": {"total_return": 0.12}}), job["id"]),
        )
        conn.execute(
            """INSERT INTO business.research_artifacts(tenant_id,job_id,name,content)
               VALUES(%s,%s,'factors',%s)""",
            (contexts[0].tenant_id, job["id"], artifact),
        )

    paper.cn_today = lambda: DAY
    paper.cn_now = lambda: datetime.combine(
        DAY,
        wall_time(14, 0),
        tzinfo=CN_TZ,
    )
    storage = TenantPaper(cluster.db, contexts[0], public / "current")
    paper.create_account(storage, 100_000, slippage_bps=0)
    order, error = paper.create_order(
        storage,
        SYMBOL,
        "buy",
        qty=100,
        ref_price=10,
        idempotency_key="recovery-order",
    )
    if error or not order:
        raise AssertionError(f"paper order setup failed: {error}")
    if len(paper.evaluate_intraday(storage, {SYMBOL: 10})) != 1:
        raise AssertionError("paper fill setup failed")

    return {
        "contexts": contexts,
        "documents": documents,
        "research_job_id": job["id"],
        "research_artifact_sha256": _sha256(artifact),
        "first_event": first_event,
        "paper": {
            "account": paper.get_account(storage),
            "orders": paper.load_orders(storage),
            "fills": paper.load_fills(storage),
            "positions": paper.load_positions(storage),
        },
    }


def _final_database_increment(cluster, seeded: dict) -> dict:
    seeded["documents"][0].put(
        "recovery",
        "state",
        {"tenant": "alpha", "phase": "final", "amount": 100_500},
    )
    with cluster.db.business_transaction(
        seeded["contexts"][0],
        write=True,
    ) as conn:
        sequence = append_event(
            conn,
            seeded["contexts"][0].tenant_id,
            "strategy_results_updated",
            {"phase": "final"},
        )
    return {"final_event": sequence}


def _restored_principals(restored, principals: list[dict]) -> list[dict]:
    values = []
    for principal in principals:
        account = restored.identity.current_user(principal["token"])
        if account["id"] != principal["user_id"]:
            raise AssertionError("restored session resolved the wrong account")
        values.append({**principal, "account": account})
    return values


def _verify_restored(
    restored,
    principals: list[dict],
    paths: dict[str, Path],
    expected: dict,
) -> dict:
    restored_principals = _restored_principals(restored, principals)
    contexts = [
        _context(restored, principal, f"recovery-restored-{index}")
        for index, principal in enumerate(restored_principals)
    ]
    documents = [TenantDocuments(restored.db, context) for context in contexts]
    own = documents[0].get("recovery", "state")
    other = documents[1].get("recovery", "state")
    if own != {"tenant": "alpha", "phase": "final", "amount": 100_500}:
        raise AssertionError(f"restored alpha document mismatch: {own}")
    if other != {"tenant": "beta", "phase": "initial", "amount": 200_000}:
        raise AssertionError(f"restored beta document mismatch: {other}")
    with restored.db.business_transaction(contexts[0]) as conn:
        leaked = conn.execute(
            """SELECT value FROM business.documents
               WHERE tenant_id=%s AND namespace='recovery' AND id='state'""",
            (contexts[1].tenant_id,),
        ).fetchone()
    if leaked is not None:
        raise AssertionError("RLS exposed another tenant's restored document")

    events = EventLog(documents[0]).since(0)
    phases = [
        row["payload"].get("phase")
        for row in events
        if row["kind"] == "strategy_results_updated"
        and row["payload"].get("phase")
    ]
    if phases != ["initial", "final"]:
        raise AssertionError("durable event history did not restore")

    storage = TenantPaper(restored.db, contexts[0], paths["public"] / "current")
    paper_state = {
        "account": paper.get_account(storage),
        "orders": paper.load_orders(storage),
        "fills": paper.load_fills(storage),
        "positions": paper.load_positions(storage),
    }
    if _canonical(paper_state) != _canonical(expected["paper"]):
        raise AssertionError("paper account, ledger, or projection changed on restore")

    jobs = ResearchJobs(documents[0])
    job = jobs.get(expected["research_job_id"])
    artifact = jobs.artifact(expected["research_job_id"], "factors")
    if job["state"] != "succeeded" or _sha256(artifact) != expected[
        "research_artifact_sha256"
    ]:
        raise AssertionError("research result or artifact changed on restore")

    marker = json.loads(
        (paths["public"] / "current" / ".public_release.json").read_text(
            encoding="utf-8"
        )
    )
    if marker != {"release_id": "release-002", "generation": 2}:
        raise AssertionError("restored public release pointer is not final")
    preferences = json.loads(
        (paths["platform"] / "preferences.json").read_text(encoding="utf-8")
    )
    if preferences.get("generation") != 2:
        raise AssertionError("restored platform configuration is stale")

    return {
        "identity_sessions": len(restored_principals),
        "rls_isolation": True,
        "events": len(events),
        "paper_orders": len(paper_state["orders"]),
        "paper_fills": len(paper_state["fills"]),
        "research_state": job["state"],
        "research_artifact_sha256": _sha256(artifact),
        "public_release_id": marker["release_id"],
        "platform_generation": preferences["generation"],
        "contexts": contexts,
        "documents": documents,
    }


def _reconcile_post_cutover(
    source,
    restored,
    source_paths: dict[str, Path],
    restored_paths: dict[str, Path],
    restored_verification: dict,
) -> dict:
    marker_id = "post-cutover-" + uuid.uuid4().hex
    restored_documents = restored_verification["documents"][0]
    restored_documents.put(
        "recovery_post_cutover",
        marker_id,
        {"preserved": True, "amount": 321.45},
    )
    restored_file = (
        restored_paths["private"] / "tenant-a" / f"{marker_id}.bin"
    )
    restored_file.write_bytes(b"post-cutover-private-increment")

    source_before = _database_inventory(source.control, source.data)
    restored_after = _database_inventory(restored.control, restored.data)
    blind_rollback_blocked = source_before["sha256"] != restored_after["sha256"]
    if not blind_rollback_blocked:
        raise AssertionError("rollback guard missed a post-cutover business write")

    row = restored.data.execute(
        """SELECT * FROM business.documents
           WHERE tenant_id=%s AND namespace='recovery_post_cutover' AND id=%s""",
        (
            restored_verification["contexts"][0].tenant_id,
            marker_id,
        ),
    ).fetchone()
    source.data.execute(
        """INSERT INTO business.documents
           (tenant_id,namespace,id,value,revision,updated_at)
           VALUES(%s,%s,%s,%s,%s,%s)""",
        (
            row["tenant_id"],
            row["namespace"],
            row["id"],
            Jsonb(row["value"]),
            row["revision"],
            row["updated_at"],
        ),
    )
    source_file = source_paths["private"] / "tenant-a" / restored_file.name
    shutil.copy2(restored_file, source_file)

    source_after = _database_inventory(source.control, source.data)
    if source_after != restored_after:
        raise AssertionError("post-cutover database increment was not reconciled")
    if _tree_inventory(source_paths["private"]) != _tree_inventory(
        restored_paths["private"]
    ):
        raise AssertionError("post-cutover private artifact was not reconciled")
    if (
        TenantDocuments(
            source.db,
            source.identity.context(
                restored_verification["contexts"][0].user_id
                and source.identity.current_user(
                    source._recovery_principal["token"]
                ),
                source._recovery_principal["tenant_id"],
                "rollback-reconciled",
            ),
        ).get("recovery_post_cutover", marker_id)
        != {"preserved": True, "amount": 321.45}
    ):
        raise AssertionError("reconciled source cannot read the preserved increment")
    return {
        "blind_rollback_blocked": True,
        "marker_id": marker_id,
        "database_reconciled": True,
        "private_artifact_reconciled": True,
        "post_cutover_database_sha256": restored_after["sha256"],
    }


def run(*, postgres_image: str, output: Path) -> dict:
    suffix = uuid.uuid4().hex[:10]
    postgres = "tsp-recovery-postgres-" + suffix
    started = time.time()
    report: dict[str, Any] = {
        "status": "failed",
        "scope": "isolated_joint_recovery_rehearsal",
        "limits": [
            "disposable local PostgreSQL container; not production data or a production host",
            "both restored databases share one PostgreSQL failure domain",
            "filesystem copies and a route pointer model quiesce/cutover; no real Nginx reload",
            "observed RPO/RTO are fixture evidence, not a production SLA",
        ],
        "postgres": _image_evidence(postgres_image),
        "targets": {
            "recommended_rpo_seconds": 24 * 3600,
            "recommended_rto_seconds": 2 * 3600,
        },
    }
    try:
        with tempfile.TemporaryDirectory(prefix="tsp-recovery-") as temporary:
            root = Path(temporary)
            source_paths = _seed_files(root / "source")
            traffic = root / "traffic.json"
            _write_json(traffic, {"active": "source", "write_epoch": 1})
            transitions: list[dict] = []

            with ExitStack() as resources:
                _docker(
                    "run",
                    "-d",
                    "--name",
                    postgres,
                    "-p",
                    "127.0.0.1::5432",
                    "-e",
                    f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
                    "-e",
                    "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                    postgres_image,
                )
                resources.callback(_remove_container, postgres)
                port = _published_port(postgres, 5432)
                root_dsn = (
                    f"postgresql://postgres:{POSTGRES_PASSWORD}"
                    f"@127.0.0.1:{port}/postgres"
                )
                _wait_postgres(root_dsn)

                with _cluster(
                    root_dsn,
                    8,
                    name_prefix="tsp_recovery_source_",
                    base_url="http://localhost",
                ) as source:
                    principals = _login(source, _seed(
                        source,
                        2,
                        email_prefix="recovery-source",
                    )).pop("principals")
                    source._recovery_principal = principals[0]
                    seeded = _seed_business(
                        source,
                        principals,
                        source_paths["public"],
                    )
                    initial_inventory = _database_inventory(
                        source.control,
                        source.data,
                    )
                    initial_backup = _backup(
                        postgres,
                        source,
                        source_paths,
                        root / "backup-initial",
                    )

                    final_principal = _seed(
                        source,
                        1,
                        email_prefix="recovery-final",
                    )[0]
                    principals.append(
                        {
                            **final_principal,
                            "account": source.identity._public(
                                source.control,
                                source.control.execute(
                                    "SELECT * FROM control.users WHERE id=%s",
                                    (final_principal["user_id"],),
                                ).fetchone(),
                            ),
                        }
                    )
                    _final_database_increment(source, seeded)
                    _final_file_increment(source_paths)
                    final_write_at = time.time()
                    final_inventory = _database_inventory(
                        source.control,
                        source.data,
                    )
                    final_backup = _backup(
                        postgres,
                        source,
                        source_paths,
                        root / "backup-final",
                    )
                    final_backup_at = time.time()
                    if initial_inventory["sha256"] == final_inventory["sha256"]:
                        raise AssertionError("final increment did not change database state")

                    backup_integrity = _verify_backup_integrity(
                        final_backup,
                        root / "backup-final",
                    )
                    failure_injection = _exercise_corruption_guards(
                        root_dsn=root_dsn,
                        postgres=postgres,
                        backup=final_backup,
                        backup_root=root / "backup-final",
                        scratch=root,
                        traffic=traffic,
                    )
                    _verify_backup_integrity(
                        final_backup,
                        root / "backup-final",
                    )
                    restore_started = time.time()
                    with _restored_cluster(
                        root_dsn,
                        postgres,
                        final_backup["control_dump"],
                        final_backup["data_dump"],
                    ) as restored:
                        restored_paths = {
                            name: root / "restored" / name
                            for name in ("platform", "public", "private", "edge")
                        }
                        directory_restore = {
                            name: _copy_tree(
                                root / "backup-final" / name,
                                restored_paths[name],
                            )
                            for name in restored_paths
                        }
                        restored_inventory = _database_inventory(
                            restored.control,
                            restored.data,
                        )
                        if restored_inventory != final_inventory:
                            raise AssertionError("restored database inventory differs")

                        _write_json(
                            traffic,
                            {"active": "restored", "write_epoch": 2},
                        )
                        transitions.append(
                            {
                                "active": "restored",
                                "at": time.time(),
                                "database_sha256": restored_inventory["sha256"],
                            }
                        )
                        verification = _verify_restored(
                            restored,
                            principals[:2],
                            restored_paths,
                            seeded,
                        )
                        restore_verified_at = time.time()
                        rollback = _reconcile_post_cutover(
                            source,
                            restored,
                            source_paths,
                            restored_paths,
                            verification,
                        )
                        _write_json(
                            traffic,
                            {"active": "source", "write_epoch": 3},
                        )
                        transitions.append(
                            {
                                "active": "source",
                                "at": time.time(),
                                "database_sha256": rollback[
                                    "post_cutover_database_sha256"
                                ],
                            }
                        )

                    rto = restore_verified_at - restore_started
                    capture_lag = final_backup_at - final_write_at
                    report.update(
                        {
                            "backup": {
                                "initial": initial_backup["evidence"],
                                "final": final_backup["evidence"],
                                "initial_database_sha256": initial_inventory[
                                    "sha256"
                                ],
                                "final_database_sha256": final_inventory[
                                    "sha256"
                                ],
                                "integrity": backup_integrity,
                                "quiesced_final_capture_lag_seconds": round(
                                    capture_lag,
                                    3,
                                ),
                            },
                            "restore": {
                                "database": restored_inventory,
                                "directories": directory_restore,
                                "verification": {
                                    key: value
                                    for key, value in verification.items()
                                    if key not in {"contexts", "documents"}
                                },
                                "observed_rto_seconds": round(rto, 3),
                                "observed_quiesced_rpo_missing_rows": 0,
                                "recommended_targets_met_in_fixture": (
                                    capture_lag <= 24 * 3600
                                    and rto <= 2 * 3600
                                ),
                            },
                            "cutover_and_rollback": {
                                "transitions": transitions,
                                **rollback,
                                "active_after_rehearsal": json.loads(
                                    traffic.read_text(encoding="utf-8")
                                )["active"],
                            },
                            "failure_injection": failure_injection,
                            "status": "passed",
                        }
                    )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["elapsed_seconds"] = round(time.time() - started, 3)
        report["report_sha256"] = _sha256(
            _canonical(report).encode()
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-image", default=DEFAULT_POSTGRES_IMAGE)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "../reports/p2-remaining-20261017/"
            "isolated-production-recovery.json"
        ),
    )
    args = parser.parse_args()
    result = run(
        postgres_image=args.postgres_image,
        output=args.output,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
