"""Controlled full-host backup. Installing its timer authorizes a maintenance window."""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .backup_full_host import create_backup
else:
    from backup_full_host import create_backup

SERVICES = ("api", "gateway", "research", "realtime", "notifications", "scheduled", "platform")


def compose_command(config: dict) -> list[str]:
    files = config["compose_files"]
    if not isinstance(files, list) or not files:
        raise ValueError("compose_files must be a nonempty list")
    command = ["docker", "compose", "--env-file", str(Path(config["env_file"]).resolve(strict=True))]
    for file in files:
        command.extend(["-f", str(Path(file).resolve(strict=True))])
    return command


def running_services(command: list[str], *, runner) -> list[str]:
    result = runner([*command, "ps", "--all", "--format", "json"],
                    capture_output=True, text=True, check=True, timeout=30)
    text = result.stdout.strip()
    if not text:
        return []
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in text.splitlines()]
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid Compose status")
    if any(row.get("Service") not in SERVICES for row in rows):
        raise ValueError("backup requires a dedicated full-product Compose project")
    if any(row.get("State") not in {
        "running", "restarting", "paused", "created", "exited", "dead", "removing",
    } for row in rows):
        raise ValueError("unknown Compose state; quiescence cannot be established")
    return sorted({row["Service"] for row in rows
                   if row["State"] not in {"created", "exited", "dead"}})


def scheduled_backup(config: dict, *, stop_writers: bool = False, runner=subprocess.run,
                     backup=create_backup) -> dict:
    command = compose_command(config)
    root = Path(config["output_root"]).resolve()
    includes = {name: Path(path).resolve(strict=True) for name, path in config["includes"].items()}
    required = {"platform_data", "public_market", "platform_config", "runtime_env", "platform_env"}
    if not required.issubset(includes):
        raise ValueError("full backup requires market, configuration and credential files")
    if any(root == source or root.is_relative_to(source) for source in includes.values()):
        raise ValueError("backup output must be outside included directories")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Linux scheduled maintenance is serialized even across different timers.
    import fcntl

    with (root / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = running_services(command, runner=runner)
        if original and not stop_writers:
            raise RuntimeError("writers are running; schedule an authorized maintenance window")
        try:
            if original:
                runner([*command, "stop", "--timeout", "60", *original],
                       capture_output=True, check=True, timeout=480)
            if running_services(command, runner=runner):
                raise RuntimeError("writers did not stop; no backup was created")
            target = root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            manifest = backup(
                container=config["postgres_container"], control_database=config["control_database"],
                data_database=config["data_database"], target=target, includes=includes, runner=runner,
            )
            return {"complete": manifest["complete"], "backup": str(target),
                    "created_at": manifest["created_at"]}
        finally:
            if original and stop_writers:
                runner([*command, "up", "-d", *original],
                       capture_output=True, check=True, timeout=480)


def backup_age(root: Path, *, max_hours: float = 24, now: datetime | None = None) -> dict:
    if not 0 < max_hours <= 8760:
        raise ValueError("invalid backup age threshold")
    now = now or datetime.now(UTC)
    latest = None
    invalid = []
    for path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text())
            stamp = datetime.fromisoformat(manifest["created_at"])
            if stamp.tzinfo is None or stamp > now or manifest.get("complete") is not True:
                raise ValueError("invalid backup timestamp or completion")
            for kind in ("control", "data"):
                evidence = manifest["restore_verification"][kind]
                dump = path.parent / f"{kind}.dump"
                if evidence.get("restored") is not True or evidence.get("source_matched") is not True:
                    raise ValueError("unverified business restore")
                if dump.stat().st_size != manifest["databases"][kind]["bytes"]:
                    raise ValueError("missing or truncated dump")
            latest = max(latest, stamp) if latest else stamp
        except (OSError, ValueError, KeyError, TypeError):
            invalid.append(path.parent.name)
    age = (now - latest).total_seconds() / 3600 if latest else None
    return {"ready": age is not None and age <= max_hours and not invalid,
            "latest_at": latest.isoformat() if latest else None,
            "age_hours": round(age, 3) if age is not None else None,
            "max_hours": max_hours, "invalid_backups": invalid}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--stop-writers", action="store_true")
    parser.add_argument("--check-age", type=Path)
    parser.add_argument("--max-hours", type=float, default=24)
    args = parser.parse_args()
    if bool(args.config) == bool(args.check_age):
        parser.error("choose --config or --check-age")
    if args.check_age:
        result = backup_age(args.check_age, max_hours=args.max_hours)
        print(json.dumps(result))
        raise SystemExit(0 if result["ready"] else 1)
    def terminate(signum, _frame):
        # Let the maintenance finally block attempt to resume stopped roles.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    config = json.loads(args.config.read_text())
    print(json.dumps(scheduled_backup(config, stop_writers=args.stop_writers)))


if __name__ == "__main__":
    main()
