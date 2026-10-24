"""Provision a persistent, isolated real-market candidate; never drop it on exit."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path

import polars as pl
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.cell.admin import (
    add_user,
    bootstrap_full_identity,
    migrate,
    provision_tenant,
    register_cell,
)
from app.market_scope import supported_symbol
from app.multitenancy.publication import PUBLIC_DIRS, PUBLIC_FILES
from scripts.acceptance.accept_external_sse import (
    _docker,
    _image_evidence,
    _published_port,
    _wait_postgres,
)
from scripts.acceptance.accept_full_compose import ROLES, nginx_config, wait_for, write_env
from scripts.acceptance.observe_candidate_soak import write_json


def private_json(path: Path, value) -> None:
    with path.open("x") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2)


def compose(run_dir: Path, *args: str):
    settings = json.loads((run_dir / "private/compose.json").read_text())
    return subprocess.run(
        ["docker", "compose", "--env-file", settings["budget"], "-p", settings["project"],
         *[arg for file in settings["files"] for arg in ("-f", file)], *args],
        env={**{k: v for k, v in os.environ.items() if not k.startswith("TSP_")},
             **settings["environment"]}, check=True, capture_output=True, text=True,
    )


def seed_market(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    manifest = json.loads((source / ".public_release.json").read_text())
    checksums = {}
    total = 0
    for name in sorted(manifest["files"]):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe public manifest path")
        if relative.parts[0] not in PUBLIC_DIRS and name not in PUBLIC_FILES:
            raise ValueError("non-public market file")
        path = source / name
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError("public data must not reference external files")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        with path.open("rb") as stream:
            source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        with target.open("rb") as stream:
            copied_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if source_hash != copied_hash or (
            name in manifest.get("checksums", {}) and source_hash != manifest["checksums"][name]
        ):
            raise ValueError("public source changed during copy")
        checksums[name] = copied_hash
        total += target.stat().st_size
    symbols = sorted({
        s for s in pl.read_parquet(destination / "instruments/instruments.parquet")["symbol"]
        if supported_symbol(s, "stock")
    })
    dates = sorted(path.name.removeprefix("date=") for path in (destination / "kline_daily").glob("date=*"))
    return {"source": str(source), "release_id": manifest.get("id"), "bytes": total,
            "files": len(checksums), "sha256": checksums, "symbols": symbols, "dates": dates}


def prepare(build_dir: Path, run_dir: Path, market_source: Path) -> dict:
    build_dir, run_dir = build_dir.resolve(), run_dir.resolve()
    built = json.loads((build_dir / "build.json").read_text())
    if built["status"] != "passed":
        raise ValueError("verified image build required")
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("platform", "public", "config", "socket", "private"):
        (run_dir / name).mkdir(mode=0o700 if name == "private" else 0o755)
    seed = seed_market(market_source, run_dir / "platform")
    write_json(run_dir / "seed.json", seed)
    # New provider configuration contains no original account/webhook secrets.
    write_json(run_dir / "config/preferences.json", {
        "second_quote_archive_enabled": True, "second_quote_archive_window": "full",
        "second_quote_archive_retention_days": 7,
        "realtime_quotes_enabled": True, "realtime_pull_stock": True,
        "realtime_quote_interval": 1, "minute_refresh_enabled": True,
        "minute_refresh_interval": 1, "realtime_data_provider": "rustdx",
        "full_minute_data_provider": "rustdx", "daily_data_provider": "rustdx",
        "system_notify_enabled": False, "strategy_monitor_enabled": False,
        "review_push_channels": [], "webhook_default_channels": [],
    })
    runtime = built["artifacts"]["runtime"]["id"]
    _docker("run", "--rm", "--network", "none", "--user", "0:0",
            "-v", f"{run_dir}:/candidate", "--entrypoint", "/bin/chown", runtime,
            "-R", "10001:10001", "/candidate/platform", "/candidate/public", "/candidate/config")
    project = "tsp-soak-" + uuid.uuid4().hex[:8]
    pg_password, market_token, admin_token, stream_secret = (secrets.token_hex(32) for _ in range(4))
    write_env(run_dir / "private/postgres.env", {"POSTGRES_PASSWORD": pg_password})
    write_env(run_dir / "private/platform.env", {
        "TSP_PLATFORM_MARKET_TOKEN": market_token, "TSP_PLATFORM_ADMIN_TOKEN": admin_token,
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    (run_dir / "private/runtime.env").touch(mode=0o600)
    # Longer timeout permits real queued backtests while other requests continue.
    (run_dir / "nginx.conf").write_text(
        nginx_config().replace('proxy_pass http://tsp-full-api:3018;',
                               'proxy_read_timeout 900s;\n            proxy_pass http://tsp-full-api:3018;')
    )
    deployment = build_dir / "source/deploy/full"
    environment = {
        "COMPOSE_PROJECT_NAME": project,
        "TSP_FULL_IMAGE": runtime, "TSP_COMPUTE_IMAGE": built["artifacts"]["compute"]["id"],
        "TSP_BROKER_IMAGE": built["artifacts"]["broker"]["id"],
        "TSP_FULL_ENV_FILE": str(run_dir / "private/runtime.env"),
        "TSP_PLATFORM_ENV_FILE": str(run_dir / "private/platform.env"),
        "TSP_CANDIDATE_POSTGRES_ENV_FILE": str(run_dir / "private/postgres.env"),
        "TSP_CANDIDATE_NGINX_CONFIG": str(run_dir / "nginx.conf"),
        "TSP_PLATFORM_DATA_DIR": str(run_dir / "platform"),
        "TSP_PUBLIC_MARKET_DIR": str(run_dir / "public"),
        "TSP_PLATFORM_CONFIG_DIR": str(run_dir / "config"),
        "TSP_COMPUTE_SOCKET_DIR": str(run_dir / "socket"),
        "TSP_POSTGRES_IMAGE": _image_evidence("postgres:16")["id"],
        "TSP_NGINX_IMAGE": _image_evidence("public.ecr.aws/docker/library/nginx:alpine")["id"],
    }
    private_json(run_dir / "private/compose.json", {
        "project": project, "budget": str(deployment / "acceptance.env"),
        "files": [str(deployment / name) for name in
                  ("compose.yml", "compose.isolated.yml", "compose.acceptance.yml")],
        "environment": environment,
    })
    config = json.loads(compose(run_dir, "config", "--format", "json").stdout)
    memory = sum(int(s.get("mem_limit", 0)) for s in config["services"].values()) + 1664 * 1024**2
    docker_memory = json.loads(_docker("info", "--format", "{{json .}}").stdout)["MemTotal"]
    if memory + 1536 * 1024**2 > docker_memory:
        raise ValueError("candidate plus load/observers cannot fit with 1.5 GiB reserve")
    print(f"Public data copied: {len(seed['symbols'])} symbols; starting isolated PostgreSQL", flush=True)
    compose(run_dir, "up", "-d", "postgres")
    pg = compose(run_dir, "ps", "-q", "postgres").stdout.strip()
    root_dsn = make_conninfo(host="127.0.0.1", port=_published_port(pg, 5432), dbname="postgres",
                             user="postgres", password=pg_password)
    _wait_postgres(root_dsn)
    credentials = {}
    accounts = []
    with psycopg.connect(root_dsn, autocommit=True, row_factory=dict_row) as root:
        for kind, role in (("control", "soak_identity"), ("data", "soak_business")):
            password = secrets.token_hex(32)
            name = "soak_" + kind
            root.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                sql.Identifier(role), sql.Literal(password)))
            root.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            with psycopg.connect(make_conninfo(root_dsn, dbname=name), autocommit=True,
                                 row_factory=dict_row) as admin:
                migrate(admin, kind, role, "full", target_version=6)
            credentials[kind] = make_conninfo(host="postgres", dbname=name, user=role, password=password)
    with (
        psycopg.connect(make_conninfo(root_dsn, dbname="soak_control"), autocommit=True,
                         row_factory=dict_row) as control,
        psycopg.connect(make_conninfo(root_dsn, dbname="soak_data"), autocommit=True,
                         row_factory=dict_row) as data,
    ):
        register_cell(control, "full", "http://localhost")
        bootstrap_full_identity(control)
        for index in range(100):
            email, password = f"soak-{index:03d}@example.test", secrets.token_urlsafe(24)
            user_id = add_user(control, email, password)
            tenant_id = uuid.uuid4().hex
            provision_tenant(control, data, tenant_id, f"持续验收 {index:03d}", "full", user_id)
            control.execute("UPDATE control.users SET default_tenant_id=%s WHERE id=%s", (tenant_id, user_id))
            accounts.append({"index": index, "email": email, "password": password,
                             "user_id": user_id, "tenant_id": tenant_id,
                             "symbol": seed["symbols"][index]})
    private_json(run_dir / "private/accounts.json", accounts)
    private_json(run_dir / "private/probes.json", credentials)
    # This is a newly created empty file, never an existing deployment secret.
    (run_dir / "private/runtime.env").unlink()
    write_env(run_dir / "private/runtime.env", {
        "TSP_CELL_ID": "full", "TSP_CELL_SCHEMA_VERSION": 6,
        "TSP_CELL_CONTROL_DSN": credentials["control"], "TSP_CELL_DATA_DSN": credentials["data"],
        "TSP_CELL_MARKET_URL": "http://platform:3018", "TSP_CELL_MARKET_TOKEN": market_token,
        "TSP_PLATFORM_ADMIN_TOKEN": admin_token, "TSP_CELL_STREAM_TICKET_SECRET": stream_secret,
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    print("100 independent accounts provisioned; starting candidate services", flush=True)
    compose(run_dir, "up", "-d")

    def healthy():
        values = {}
        for role in (*ROLES, "postgres", "edge", "broker"):
            cid = compose(run_dir, "ps", "--all", "-q", role).stdout.strip()
            if not cid:
                return None
            value = json.loads(_docker("inspect", cid).stdout)[0]
            values[role] = {
                "id": cid, "name": value["Name"].lstrip("/"), "image": value["Image"],
                "health": value["State"].get("Health", {}).get("Status"),
                "memory_limit": value["HostConfig"]["Memory"],
            }
        return values if all(v["health"] == "healthy" for v in values.values()) else None

    services = wait_for(healthy, timeout=480)
    host_url = f"http://127.0.0.1:{_published_port(services['edge']['id'], 8080)}"
    with psycopg.connect(make_conninfo(root_dsn, dbname="soak_control"), autocommit=True,
                         row_factory=dict_row) as control:
        register_cell(control, "full", host_url)
    result = {
        "status": "ready_for_smoke", "project": project, "build": built,
        "services": services, "budget_including_compute_and_observers_bytes": memory,
        "host_url": host_url,
        "independent_users": len(accounts), "independent_tenants": len({p["tenant_id"] for p in accounts}),
        "market_seed": {k: seed[k] for k in ("source", "release_id", "bytes", "files")},
        "limitations": ["loopback HTTP, no public TLS", "ARM64, not AMD64 or Windows",
                        "bounded one-compute capacity; no live orders or external notifications"],
    }
    write_json(run_dir / "deployment.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--market-source", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.build_dir, args.run_dir, args.market_source)
    print(json.dumps({k: result[k] for k in ("status", "project", "host_url", "independent_users")}))


if __name__ == "__main__":
    main()
