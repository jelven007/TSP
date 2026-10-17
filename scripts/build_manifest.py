#!/usr/bin/env python3
"""Offline build identity, file verification and upgrade backups; never restores code."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

EXCLUDED_DIRS = {
    ".git", ".dbg", ".venv", "venv", "node_modules", "__pycache__", "data", "logs",
    ".pytest_cache", ".ruff_cache", "target",
}
EXCLUDED_NAMES = {".DS_Store", "build-manifest.json"}
MAX_FILES = 100_000
MAX_BACKUP_BYTES = 256 * 1024 * 1024
SOURCE_SCOPES = [
    "backend/app", "backend/native/rustdx_native/src", "backend/native/rustdx_native/Cargo.toml",
    "backend/native/rustdx_native/Cargo.lock", "backend/native/rustdx_native/pyproject.toml",
    "backend/pyproject.toml", "backend/uv.lock", "frontend/src", "frontend/public",
    "frontend/package.json", "frontend/pnpm-lock.yaml", "frontend/index.html",
    "frontend/vite.config.ts", "frontend/tsconfig.json", "frontend/tsconfig.node.json",
    "frontend/tailwind.config.ts", "frontend/postcss.config.js",
    "packaging", "scripts", "Dockerfile", ".dockerignore", "tiers.yaml", "VERSION",
]


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: str) -> str:
    if (
        not isinstance(value, str) or not value or "\\" in value or ":" in value
        or any(ord(c) < 32 for c in value) or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"invalid relative path: {value!r}")
    return value


def _excluded(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    if not parts:
        return True
    return (
        any(part in EXCLUDED_DIRS for part in parts)
        or any(part == ".env" or part.startswith(".env.") for part in parts)
        or parts[-1] in EXCLUDED_NAMES
        or parts[-1].endswith((".pyc", ".pyo", ".log", ".tmp"))
    )


def _safe_path(root: Path, rel: str) -> Path:
    path = root / _relative(rel)
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"symlink parent: {rel}")
    return path


def _entry(root: Path, rel: str) -> dict:
    path = _safe_path(root, rel)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        if os.path.isabs(target) or not path.resolve().is_relative_to(root) or not path.exists():
            raise ValueError(f"unsafe or dangling symlink: {rel}")
        return {"kind": "symlink", "target": target, "sha256": _hash(target.encode())}
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not a regular file: {rel}")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
            raise ValueError(f"file changed while opening: {rel}")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"file changed while hashing: {rel}")
    return {"kind": "file", "size": info.st_size, "sha256": digest.hexdigest()}


def _read_expected(root: Path, rel: str, expected: dict) -> bytes:
    descriptor = os.open(_safe_path(root, rel), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected["size"]:
            raise ValueError(f"file changed while backing up: {rel}")
        data = stream.read(min(info.st_size, MAX_BACKUP_BYTES) + 1)
    if len(data) > MAX_BACKUP_BYTES or _hash(data) != expected["sha256"]:
        raise ValueError(f"file changed while backing up: {rel}")
    return data


def _scan(root: Path, scopes: list[str], *, missing_ok: bool = False) -> dict:
    files = {}
    for scope in scopes:
        path = _safe_path(root, scope)
        if _excluded(scope):
            raise ValueError(f"excluded scope: {scope}")
        if not path.exists() and not path.is_symlink():
            if missing_ok:
                continue
            raise ValueError(f"missing scope: {scope}")
        if path.is_symlink() or not path.is_dir():
            paths = [scope]
        else:
            paths = []
            for directory, dirs, names in os.walk(path, followlinks=False):
                parent = Path(directory)
                for name in list(dirs):
                    child = parent / name
                    rel = child.relative_to(root).as_posix()
                    if _excluded(rel):
                        dirs.remove(name)
                    elif child.is_symlink():
                        dirs.remove(name)
                        paths.append(rel)
                paths.extend((parent / name).relative_to(root).as_posix() for name in names)
                if len(paths) > MAX_FILES:
                    raise ValueError("too many files")
        for rel in paths:
            if not _excluded(rel):
                files[rel] = _entry(root, rel)
                if len(files) > MAX_FILES:
                    raise ValueError("too many files")
    for rel, entry in files.items():
        if entry["kind"] == "symlink":
            target = (root / rel).resolve().relative_to(root).as_posix()
            if _excluded(target) or not any(
                target == scope or target.startswith(scope + "/") for scope in scopes
            ):
                raise ValueError(f"symlink target outside manifest scopes: {rel}")
    return dict(sorted(files.items()))


def create(root: Path, scopes: list[str], *, version: str, commit: str | None) -> dict:
    if commit is not None and not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit must be a full 40-character SHA or unknown")
    if not re.fullmatch(r"[0-9]\.[0-9]\.(?:0|[1-9][0-9]?)", version):
        raise ValueError("invalid application version")
    scopes = sorted(set(_relative(scope) for scope in scopes))
    if not scopes:
        raise ValueError("at least one file scope is required")
    files = _scan(root.resolve(), scopes)
    return {
        "format": 1, "version": version, "commit": commit,
        "commit_origin": "supplied" if commit else "unknown", "source_dirty": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scopes": scopes, "files": files, "files_sha256": _hash(_json(files)),
    }


def _validate(payload: dict) -> None:
    if not isinstance(payload, dict) or payload.get("format") != 1:
        raise ValueError("unsupported manifest format")
    scopes = payload.get("scopes")
    files = payload.get("files")
    if not isinstance(scopes, list) or not scopes or not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError("invalid manifest scopes/files")
    for scope in scopes:
        if _excluded(_relative(scope)):
            raise ValueError("excluded manifest scope")
    for rel, entry in files.items():
        _relative(rel)
        if _excluded(rel) or not any(rel == scope or rel.startswith(scope + "/") for scope in scopes):
            raise ValueError("file outside manifest scope")
        if not isinstance(entry, dict) or entry.get("kind") not in {"file", "symlink"}:
            raise ValueError("invalid file entry")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise ValueError("invalid file hash")
    if payload.get("files_sha256") != _hash(_json(files)):
        raise ValueError("manifest file index hash mismatch")


def _differences(actual: dict, expected: dict) -> dict:
    return {
        "changed": sorted(p for p in actual.keys() & expected.keys() if actual[p] != expected[p]),
        "missing": sorted(expected.keys() - actual.keys()),
        "added": sorted(actual.keys() - expected.keys()),
    }


def verify(root: Path, payload: dict) -> dict:
    _validate(payload)
    return _differences(_scan(root.resolve(), payload["scopes"], missing_ok=True), payload["files"])


def new_output(output: Path) -> None:
    """Only create a fresh directory, without following an output ancestor symlink."""
    output = output.absolute()
    if any(parent.is_symlink() for parent in (output, *output.parents)):
        raise ValueError("symlink output path")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)


def backup(root: Path, before: dict, candidate: dict, output: Path) -> dict:
    _validate(before)
    _validate(candidate)
    root = root.resolve()
    actual = _scan(root, before["scopes"], missing_ok=True)
    changes = _differences(actual, before["files"])
    paths = sorted(set(sum(changes.values(), [])))
    if sum(actual[p].get("size", 0) for p in paths if p in actual) > MAX_BACKUP_BYTES:
        raise ValueError("backup exceeds 256 MiB")
    # Output cannot become an input during the final stable-snapshot check.
    resolved_output = output.absolute()
    for scope in before["scopes"]:
        if resolved_output == root / scope or resolved_output.is_relative_to(root / scope):
            raise ValueError("backup output overlaps file scopes")
    new_output(output)
    categories = {}
    with zipfile.ZipFile(output / "local-files.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel in paths:
            old, local, new = before["files"].get(rel), actual.get(rel), candidate["files"].get(rel)
            if local == new:
                category = "matches_candidate"
            elif old is None and new is None:
                category = "local_added"
            elif old == new:
                category = "local_deleted" if local is None else "candidate_unchanged"
            else:
                category = "conflict"
            categories[rel] = category
            if local is not None:
                # Never recreate symlinks in the backup; record their literal targets.
                if local["kind"] == "symlink":
                    data = local["target"].encode()
                else:
                    data = _read_expected(root, rel, local)
                if len(data) > MAX_BACKUP_BYTES or _hash(data) != local["sha256"]:
                    raise ValueError(f"file changed while backing up: {rel}")
                archive.writestr(rel, data)
    if _scan(root, before["scopes"], missing_ok=True) != actual:
        raise ValueError("files changed while backing up; incomplete backup")
    (output / "baseline.json").write_bytes(_json(before))
    (output / "candidate.json").write_bytes(_json(candidate))
    report = {
        "format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "files": categories,
        "local_entries": {p: actual.get(p) for p in paths},
        "baseline_commit": before.get("commit"), "candidate_commit": candidate.get("commit"),
        "archive_sha256": _entry(output.resolve(), "local-files.zip")["sha256"],
        "complete": True, "restores_code": False,
    }
    # Written last: partial output never advertises a complete backup.
    (output / "report.json").write_bytes(_json(report))
    return report


def seal(root: Path, payload: dict, artifact: Path, commit: str) -> dict:
    """Attach installer identity only after rechecking the packaged runtime."""
    if payload.get("commit") != commit or payload.get("source_dirty") is not False:
        raise ValueError("release requires a clean, matching source commit")
    if any(verify(root, payload).values()):
        raise ValueError("runtime changed after packaging")
    return {**payload, "artifact": {
        "name": artifact.name, **_entry(artifact.parent.resolve(), artifact.name),
    }}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create")
    make.add_argument("--root", type=Path, required=True)
    make.add_argument("--include", action="append", default=[])
    make.add_argument("--profile", choices=["source"])
    make.add_argument("--version")
    make.add_argument("--version-file", type=Path)
    make.add_argument("--commit")
    make.add_argument("--git-root", type=Path)
    make.add_argument("--output", type=Path, required=True)
    for name in ("verify", "backup", "seal"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        if name == "backup":
            command.add_argument("--candidate", type=Path, required=True)
        if name == "seal":
            command.add_argument("--artifact", type=Path, required=True)
            command.add_argument("--commit", required=True)
        if name != "verify":
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            commit = args.commit or None
            dirty = None
            if args.git_root:
                def git(*values):
                    return subprocess.check_output(
                        ["git", *values], cwd=args.git_root, text=True,
                    ).strip()
                resolved = git("rev-parse", "HEAD")
                if commit and resolved != commit:
                    raise ValueError("supplied commit differs from checkout")
                commit = resolved
                dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
            version = args.version or (
                args.version_file.read_text().strip().removeprefix("v") if args.version_file else ""
            )
            payload = create(
                args.root, SOURCE_SCOPES if args.profile == "source" else args.include,
                version=version, commit=commit,
            )
            payload["source_dirty"] = dirty
            if args.git_root:
                payload["commit_origin"] = "git"
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("xb") as stream:
                stream.write(_json(payload))
            print(json.dumps({"files": len(payload["files"]), "files_sha256": payload["files_sha256"]}))
        else:
            payload = json.loads(args.manifest.read_bytes())
            if args.command == "verify":
                differences = verify(args.root, payload)
                print(json.dumps(differences, ensure_ascii=False))
                return 2 if any(differences.values()) else 0
            if args.command == "seal":
                report = seal(args.root, payload, args.artifact, args.commit)
                with args.output.open("xb") as stream:
                    stream.write(_json(report))
                print(json.dumps(report["artifact"], ensure_ascii=False))
            else:
                report = backup(args.root, payload, json.loads(args.candidate.read_bytes()), args.output)
                print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Build/upgrade verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
