"""Conservative public-release and Docker-cache maintenance for one host."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

RELEASE_ID = re.compile(r"^[0-9a-f]{32}$")
REFERENCE_QUERY = (
    "SELECT coalesce(json_agg(DISTINCT release_id), '[]') FROM ("
    "SELECT metadata#>>'{research,public_release_id}' AS release_id "
    "FROM business.research_jobs "
    "UNION SELECT input->>'public_release_id' FROM business.research_jobs"
    ") references_all WHERE release_id IS NOT NULL;"
)


def referenced_releases_from_postgres(container: str, database: str) -> set[str]:
    # The host runs psql as the database owner so RLS cannot hide other tenants'
    # retained jobs. Credentials stay inside the existing PostgreSQL container.
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", container):
        raise ValueError("invalid PostgreSQL container")
    if not re.fullmatch(r"[a-zA-Z0-9_]+", database):
        raise ValueError("invalid business database")
    try:
        result = subprocess.run(
            [
                "docker", "exec", "-i", container, "sh", "-c",
                'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -At',
                "psql", database,
            ],
            input="SET statement_timeout = '15s';\n" + REFERENCE_QUERY,
            capture_output=True, text=True, check=True, timeout=25,
        )
    except (subprocess.SubprocessError, OSError):
        raise RuntimeError("research release query failed") from None
    # SET emits a command tag; the JSON aggregate is the final line.
    lines = result.stdout.strip().splitlines()
    values = json.loads(lines[-1] if lines else "")
    if not isinstance(values, list) or any(
        value is not None and (not isinstance(value, str) or not RELEASE_ID.fullmatch(value))
        for value in values
    ):
        raise ValueError("invalid research release references")
    return {value for value in values if value is not None}


def referenced_releases(dsn: str) -> set[str]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """SELECT metadata#>>'{research,public_release_id}' AS release_id
               FROM business.research_jobs
               UNION SELECT input->>'public_release_id' FROM business.research_jobs""",
        ).fetchall()
    return {
        row[0] for row in rows
        if isinstance(row[0], str) and RELEASE_ID.fullmatch(row[0])
    }


def release_candidates(
    root: Path,
    *,
    protected: set[str],
    keep: int,
    min_age_days: int = 30,
    min_age_hours: int | None = None,
    now: float | None = None,
) -> list[Path]:
    if keep < 2 or min_age_days < 1:
        raise ValueError("release retention must keep at least two and one day")
    if min_age_hours is not None and min_age_hours < 1:
        raise ValueError("release retention must keep at least one hour")
    root = root.resolve(strict=True)
    releases = (root / "releases").resolve(strict=True)
    if not releases.is_relative_to(root):
        raise ValueError("release directory escapes root")
    current = (root / "current").resolve(strict=True)
    if not current.is_relative_to(releases):
        raise ValueError("current release escapes release directory")
    values = sorted(
        (
            path for path in releases.iterdir()
            if path.is_dir() and not path.is_symlink() and RELEASE_ID.fullmatch(path.name)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    retained = {path.name for path in values[:keep]} | protected | {current.name}
    age_seconds = min_age_days * 86400 if min_age_hours is None else min_age_hours * 3600
    cutoff = (now if now is not None else time.time()) - age_seconds
    return [
        path for path in values
        if path.name not in retained and path.stat().st_mtime < cutoff
    ]


def docker_prune_commands(*, until: str, keep_storage: str) -> list[list[str]]:
    if not re.fullmatch(r"[1-9][0-9]*[hm]", until):
        raise ValueError("invalid Docker retention duration")
    if not re.fullmatch(r"[1-9][0-9]*(?:GB|MB)", keep_storage):
        raise ValueError("invalid BuildKit storage budget")
    return [
        [
            "docker", "builder", "prune", "--force",
            "--filter", f"until={until}", "--keep-storage", keep_storage,
        ],
        ["docker", "image", "prune", "--force", "--filter", f"until={until}"],
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--keep-releases", type=int, default=30)
    parser.add_argument("--min-release-age-days", type=int, default=30)
    parser.add_argument("--min-release-age-hours", type=int)
    parser.add_argument("--data-admin-dsn-env", default="TSP_DATA_ADMIN_DSN")
    parser.add_argument("--postgres-container")
    parser.add_argument("--business-database")
    parser.add_argument("--protect-release", action="append", default=[])
    parser.add_argument("--prune-docker-cache", action="store_true")
    parser.add_argument("--docker-until", default="168h")
    parser.add_argument("--builder-keep-storage", default="20GB")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if bool(args.postgres_container) != bool(args.business_database):
        parser.error("--postgres-container and --business-database must be supplied together")

    dsn = os.environ.get(args.data_admin_dsn_env, "")
    protected = set(args.protect_release)
    invalid = sorted(value for value in protected if not RELEASE_ID.fullmatch(value))
    if invalid:
        parser.error(f"invalid protected release: {invalid[0]}")
    report = {
        "applied": args.apply,
        "release_root": str(args.release_root.resolve()),
        "protected_releases": sorted(protected),
        "release_candidates": [],
        "docker_commands": [],
    }
    if dsn or args.postgres_container:
        def read_references():
            return (
                referenced_releases_from_postgres(args.postgres_container, args.business_database)
                if args.postgres_container else referenced_releases(dsn)
            )

        protected.update(read_references())
        candidates = release_candidates(
            args.release_root,
            protected=protected,
            keep=args.keep_releases,
            min_age_days=args.min_release_age_days,
            min_age_hours=args.min_release_age_hours,
        )
        report["protected_releases"] = sorted(protected)
        report["release_candidates"] = [path.name for path in candidates]
        if args.apply:
            report["deleted_releases"] = []
            for offset in range(0, len(candidates), 30):
                # Recheck current/recent releases and new research references
                # between bounded batches while the publisher remains active.
                protected.update(read_references())
                available = set(release_candidates(
                    args.release_root, protected=protected,
                    keep=args.keep_releases, min_age_days=args.min_release_age_days,
                    min_age_hours=args.min_release_age_hours,
                ))
                for path in candidates[offset:offset + 30]:
                    if path not in available:
                        continue
                    if path.is_symlink() or path.resolve().parent != (
                        args.release_root.resolve() / "releases"
                    ):
                        raise ValueError("release target changed during maintenance")
                    shutil.rmtree(path)
                    report["deleted_releases"].append(path.name)
    else:
        report["release_prune_skipped"] = (
            f"{args.data_admin_dsn_env} or a PostgreSQL container/database is required "
            "to protect retained research jobs"
        )

    if args.prune_docker_cache:
        commands = docker_prune_commands(
            until=args.docker_until,
            keep_storage=args.builder_keep_storage,
        )
        report["docker_commands"] = commands
        if args.apply:
            for command in commands:
                subprocess.run(command, check=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
