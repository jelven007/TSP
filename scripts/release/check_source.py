"""Check a development tree against an explicit Git or recovery source identity."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.common import file_hash, identity
from release.source_recovery import archive_members, file_version, verify

# Only untracked additions are excluded. Every archived member is still checked.
EXCLUDED = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".cache", ".vite", "dist", "data", "logs",
}


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "--no-optional-locks", *args], cwd=root, text=True,
        stderr=subprocess.PIPE,
    ).strip()


def check(root: Path, version: str, revision: str, recovery: Path | None = None) -> dict:
    identity(version, revision)
    root = root.resolve()
    if (root / "VERSION").is_symlink():
        raise ValueError("VERSION must not be a symlink")
    if file_version((root / "VERSION").read_text()) != version:
        raise ValueError("working VERSION mismatch")
    report = {
        "version": version, "expected_revision": revision,
        "root": str(root), "git_head": None, "runtime_verified": False,
        "remote_verified": False,
    }
    if recovery is None:
        if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("root must be the Git working tree root")
        head = git(root, "rev-parse", "--verify", "HEAD^{commit}")
        if head != revision or file_version(git(root, "show", f"{head}:VERSION")) != version:
            raise ValueError("Git HEAD or committed VERSION mismatch")
        # Preserve porcelain spacing; this is status, not file content.
        status = subprocess.check_output(
            ["git", "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=root, text=True, stderr=subprocess.PIPE,
        )
        report.update(basis="git_head", git_head=head, clean=not status,
                      git_status=status.splitlines())
        return report

    recovery = recovery.absolute()
    manifest = verify(recovery)
    records = [row for row in manifest["versions"]
               if row["version"] == version and row["commit"] == revision]
    if len(records) != 1:
        raise ValueError("recovery version/revision mismatch")
    record = records[0]
    members = archive_members(recovery / record["archive"], version)
    changes = []
    for name, expected in sorted(members.items()):
        path = root / name
        if path.is_symlink() or any(parent.is_symlink()
                                    for parent in path.parents if parent != root):
            kind = "symlink"
        elif not path.is_file():
            kind = "missing"
        elif file_hash(path) != expected["sha256"]:
            kind = "modified"
        else:
            continue
        changes.append({"path": name, "kind": kind})

    def walk_error(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED)
        # Do not follow directory symlinks or hash local secrets.
        names = files + [name for name in dirs if (Path(directory) / name).is_symlink()]
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if (relative not in members and not any(key.startswith(relative + "/") for key in members)
                    and name != ".DS_Store" and not name.endswith(".tsbuildinfo")):
                changes.append({"path": relative, "kind": "added"})
    report.update(
        basis="verified_recovery_archive", archive_sha256=record["sha256"],
        checked_files=len(members), clean=not changes,
        changes=sorted(changes, key=lambda row: row["path"]),
        excluded_untracked_directories=sorted(EXCLUDED),
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True, help="expected full 40-character commit")
    parser.add_argument("--recovery-set", type=Path, help="trusted source_recovery output directory")
    parser.add_argument("--allow-dirty", action="store_true", help="report source drift without failing")
    args = parser.parse_args()
    try:
        report = check(args.root, args.version, args.revision, args.recovery_set)
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError,
            subprocess.CalledProcessError) as exc:
        parser.exit(2, f"source check failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["clean"] or args.allow_dirty else 1)


if __name__ == "__main__":
    main()
