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


def referenced_releases(dsn: str) -> set[str]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """SELECT DISTINCT metadata#>>'{research,public_release_id}' AS release_id
               FROM business.research_jobs
               WHERE metadata#>>'{research,public_release_id}' IS NOT NULL""",
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
    min_age_days: int,
    now: float | None = None,
) -> list[Path]:
    if keep < 2 or min_age_days < 1:
        raise ValueError("release retention must keep at least two and one day")
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
    cutoff = (now if now is not None else time.time()) - min_age_days * 86400
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
    parser.add_argument("--data-admin-dsn-env", default="TSP_DATA_ADMIN_DSN")
    parser.add_argument("--protect-release", action="append", default=[])
    parser.add_argument("--prune-docker-cache", action="store_true")
    parser.add_argument("--docker-until", default="168h")
    parser.add_argument("--builder-keep-storage", default="20GB")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

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
    if dsn:
        protected.update(referenced_releases(dsn))
        candidates = release_candidates(
            args.release_root,
            protected=protected,
            keep=args.keep_releases,
            min_age_days=args.min_release_age_days,
        )
        report["protected_releases"] = sorted(protected)
        report["release_candidates"] = [path.name for path in candidates]
        if args.apply:
            for path in candidates:
                shutil.rmtree(path)
    else:
        report["release_prune_skipped"] = (
            f"{args.data_admin_dsn_env} is required to protect retained research jobs"
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
