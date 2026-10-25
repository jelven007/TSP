"""Create a quiesced two-database backup and prove both dumps restore."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

BUSINESS_CHECKS = {
    "control": {
        "users": "SELECT count(*) FROM control.users",
        "tenants": "SELECT count(*) FROM control.tenants",
        "memberships": "SELECT count(*) FROM control.memberships",
        "admin_roles": "SELECT count(*) FROM control.user_roles WHERE role_id='admin'",
        "registration": "SELECT count(*) FROM control.registration_config",
    },
    "business": {
        "bindings": "SELECT count(*) FROM business.tenant_bindings",
        "documents": "SELECT count(*) FROM business.documents",
        "research_jobs": "SELECT count(*) FROM business.research_jobs",
        "artifact_bytes": "SELECT coalesce(sum(octet_length(content)),0) FROM business.research_artifacts",
        "paper_accounts": "SELECT count(*) FROM business.full_paper_accounts",
        "paper_orders": "SELECT count(*) FROM business.full_paper_orders",
        "paper_fills": "SELECT count(*) FROM business.full_paper_fills",
        "paper_nav": "SELECT count(*) FROM business.full_paper_nav",
        "cash": "SELECT coalesce(sum((value->>'cash')::numeric),0)::text FROM business.full_paper_accounts",
    },
}


def business_evidence(container: str, database: str, schema: str, *, runner) -> dict:
    checks = BUSINESS_CHECKS[schema]
    fields = ",".join(f"'{name}',({query})" for name, query in checks.items())
    query = f"SET statement_timeout='60s'; SELECT json_build_object({fields});"
    result = run_checked([
        "docker", "exec", container, "psql", "-X", "--set", "ON_ERROR_STOP=1",
        "--username", "postgres", "--dbname", database,
        "--tuples-only", "--no-align", "--command", query,
    ], runner=runner, capture_output=True, text=True)
    value = json.loads(result.stdout.strip().splitlines()[-1])
    if not isinstance(value, dict) or set(value) != set(checks):
        raise RuntimeError("invalid backup business evidence")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def file_evidence(path: Path) -> dict:
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def tree_evidence(root: Path) -> dict:
    digest = hashlib.sha256()
    files = 0
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            digest.update(f"L:{relative}:{os.readlink(path)}\n".encode())
        elif path.is_file():
            evidence = file_evidence(path)
            digest.update(
                f"F:{relative}:{evidence['bytes']}:{evidence['sha256']}\n".encode(),
            )
            files += 1
            total += evidence["bytes"]
        elif path.is_dir():
            digest.update(f"D:{relative}\n".encode())
    return {"files": files, "bytes": total, "sha256": digest.hexdigest()}


def run_checked(command: list[str], *, runner, **kwargs):
    result = runner(command, **kwargs)
    if result.returncode:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise RuntimeError(f"{' '.join(command[:4])} failed: {(stderr or '')[-2000:]}")
    return result


def dump_database(container: str, database: str, target: Path, *, runner) -> dict:
    with target.open("xb") as output:
        result = runner([
            "docker", "exec", container, "pg_dump", "--format=custom",
            "--no-owner", "--no-acl", "--username", "postgres", "--dbname", database,
        ], stdout=output, stderr=subprocess.PIPE)
    if result.returncode:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed for {database}")
    target.chmod(0o600)
    with target.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise RuntimeError(f"invalid PostgreSQL dump for {database}")
    return file_evidence(target)


def verify_restore(
    container: str, database: str, dump: Path, schema: str, *, runner, expected: dict | None = None,
) -> dict:
    scratch = "tsp_verify_" + uuid.uuid4().hex[:16]
    run_checked([
        "docker", "exec", container, "createdb", "--username", "postgres", scratch,
    ], runner=runner, capture_output=True)
    try:
        with dump.open("rb") as source:
            run_checked([
                "docker", "exec", "-i", container, "pg_restore", "--exit-on-error",
                "--no-owner", "--no-acl", "--username", "postgres", "--dbname", scratch,
            ], runner=runner, stdin=source, capture_output=True)
        query = (
            f"SELECT max(version),"
            f"(SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='{schema}') "
            f"FROM {schema}.schema_version"
        )
        result = run_checked([
            "docker", "exec", container, "psql", "--username", "postgres",
            "--dbname", scratch, "--tuples-only", "--no-align", "--command", query,
        ], runner=runner, capture_output=True, text=True)
        version, tables = result.stdout.strip().split("|", 1)
        observed = business_evidence(container, scratch, schema, runner=runner)
        if expected is not None and observed != expected:
            raise RuntimeError("restored business evidence differs from the quiesced source")
        return {
            "restored": True,
            "schema": schema,
            "schema_version": int(version),
            "table_count": int(tables),
            "business_evidence": observed,
            "source_matched": expected is not None,
        }
    finally:
        run_checked([
            "docker", "exec", container, "dropdb", "--force",
            "--username", "postgres", scratch,
        ], runner=runner, capture_output=True)


def parse_include(value: str) -> tuple[str, Path]:
    name, separator, source = value.partition("=")
    if not separator or not NAME.fullmatch(name) or not source:
        raise ValueError(f"invalid include: {value}")
    return name, Path(source).resolve(strict=True)


def create_backup(
    *,
    container: str,
    control_database: str,
    data_database: str,
    target: Path,
    includes: dict[str, Path],
    runner=subprocess.run,
) -> dict:
    if any(not NAME.fullmatch(name) for name in includes):
        raise ValueError("invalid backup include name")
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name("." + target.name + "." + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        expected = {
            schema: business_evidence(container, database, schema, runner=runner)
            for schema, database in (("control", control_database), ("business", data_database))
        }
        dumps = {
            "control": dump_database(
                container, control_database, stage / "control.dump", runner=runner,
            ),
            "data": dump_database(
                container, data_database, stage / "data.dump", runner=runner,
            ),
        }
        copied = {}
        for name, source in includes.items():
            destination = stage / "files" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
                source_evidence = tree_evidence(source)
                retained_evidence = tree_evidence(destination)
            else:
                shutil.copy2(source, destination)
                source_evidence = file_evidence(source)
                retained_evidence = file_evidence(destination)
            if source_evidence != retained_evidence:
                raise RuntimeError(f"backup include changed while copying: {name}")
            copied[name] = {
                "source": str(source),
                "kind": "directory" if source.is_dir() else "file",
                "evidence": retained_evidence,
            }
        restore = {
            "control": verify_restore(
                container, control_database, stage / "control.dump", "control", runner=runner,
                expected=expected["control"],
            ),
            "data": verify_restore(
                container, data_database, stage / "data.dump", "business", runner=runner,
                expected=expected["business"],
            ),
        }
        manifest = {
            "complete": True,
            "created_at": datetime.now(UTC).isoformat(),
            "quiesced": True,
            "databases": {
                "control": {"name": control_database, **dumps["control"]},
                "data": {"name": data_database, **dumps["data"]},
            },
            "includes": copied,
            "restore_verification": restore,
        }
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        os.replace(stage, target)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-container", required=True)
    parser.add_argument("--control-database", required=True)
    parser.add_argument("--data-database", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--confirm-quiesced", action="store_true")
    args = parser.parse_args()
    if not args.confirm_quiesced:
        parser.error("stop all writers and pass --confirm-quiesced")
    includes = dict(parse_include(value) for value in args.include)
    manifest = create_backup(
        container=args.postgres_container,
        control_database=args.control_database,
        data_database=args.data_database,
        target=args.output.resolve(),
        includes=includes,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
