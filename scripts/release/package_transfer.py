#!/usr/bin/env python3
"""Metadata-free Mac/Linux tar transfer, with verification before extraction."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import posixpath
import re
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

MAX_MEMBERS = 100_000
PAX_KEYS = {"path", "linkpath", "size", "mtime", "comment"}


def metadata_path(name: str) -> bool:
    return any(part in {".DS_Store", "__MACOSX"} or part.startswith("._")
               for part in PurePosixPath(name).parts)


def reject_metadata(member: tarfile.TarInfo) -> None:
    """Shared admission guard, including resolved global PAX headers."""
    if metadata_path(member.name) or metadata_path(member.linkname):
        raise ValueError(f"macOS metadata member: {member.name}")
    if set(member.pax_headers) - PAX_KEYS:
        raise ValueError(f"archive metadata headers: {member.name}")


def canonical(name: str) -> str:
    if (not isinstance(name, str) or not name or name.startswith("/")
            or "\\" in name or ":" in name or any(ord(c) < 32 for c in name)
            or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise ValueError(f"unsafe archive path: {name!r}")
    if len(name.encode("utf-8")) > 4095 or any(
        len(part.encode("utf-8")) > 255 for part in name.split("/")
    ):
        raise ValueError(f"archive path exceeds Linux filename limits: {name!r}")
    return name


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def index_digest(entries: dict) -> str:
    return hashlib.sha256(json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def validate_links(entries: dict) -> None:
    for name in entries:
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and entries.get(str(parent), {}).get("kind") != "directory":
                raise ValueError(f"missing or non-directory archive parent: {name}")

    def resolve(name: str, visited: set) -> str:
        parts = name.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            entry = entries.get(prefix)
            if entry is None:
                raise ValueError(f"dangling archive link: {name}")
            if entry["kind"] == "symlink":
                if prefix in visited:
                    raise ValueError(f"cyclic archive link: {name}")
                target = entry["target"]
                if (not target or target.startswith("/") or "\\" in target or ":" in target
                        or any(ord(c) < 32 for c in target) or metadata_path(target)):
                    raise ValueError(f"unsafe archive link: {prefix}")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(prefix), target))
                if resolved == ".." or resolved.startswith("../"):
                    raise ValueError(f"escaping archive link: {prefix}")
                suffix = parts[i:]
                return resolve("/".join([resolved, *suffix]), visited | {prefix})
            if i < len(parts) and entry["kind"] != "directory":
                raise ValueError(f"non-directory link target: {name}")
        return name

    for name, entry in entries.items():
        if entry["kind"] == "symlink":
            target = resolve(name, set())
            if target.split("/")[0] != name.split("/")[0]:
                raise ValueError(f"link outside package root: {name}")


def scan(root: Path, *, filter_metadata: bool = False) -> tuple[dict, list]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("package tree must be a real directory")
    root = root.resolve()
    entries, excluded = {}, []

    def visit(path: Path) -> None:
        name = path.relative_to(root).as_posix()
        if metadata_path(name):
            if not filter_metadata:
                raise ValueError(f"macOS metadata in package tree: {name}")
            excluded.append(name)
            return
        canonical(name)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            entries[name] = {"kind": "symlink", "target": os.readlink(path)}
        elif stat.S_ISDIR(info.st_mode):
            entries[name] = {"kind": "directory", "mode": 0o755}
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
                after = os.fstat(stream.fileno())
            if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError(f"source changed while hashing: {name}")
            entries[name] = {"kind": "file", "size": info.st_size, "sha256": sha,
                             "mode": 0o644 | (stat.S_IMODE(info.st_mode) & 0o111)}
        else:
            raise ValueError(f"unsupported package file: {name}")
        if len(entries) > MAX_MEMBERS:
            raise ValueError("too many package members")

    for child in sorted(root.iterdir()):
        visit(child)
    # A synthetic root preserves the same link boundary as the final tar.
    wrapped = {"package": {"kind": "directory", "mode": 0o755}}
    wrapped.update({f"package/{name}": entry for name, entry in entries.items()})
    validate_links(wrapped)
    return dict(sorted(entries.items())), excluded


def verify(archive: Path, sha256: str | None = None) -> dict:
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("archive must be a regular file")
    actual_sha = digest(archive)
    if sha256 is not None and (not re.fullmatch(r"[0-9a-f]{64}", sha256) or sha256 != actual_sha):
        raise ValueError("archive SHA-256 mismatch")
    entries = {}
    with gzip.open(archive, "rb") as compressed, tarfile.open(fileobj=compressed, mode="r:") as source:
        for member in source:
            reject_metadata(member)
            name = canonical(member.name.rstrip("/") if member.isdir() else member.name)
            if name in entries:
                raise ValueError(f"duplicate archive member: {name}")
            if member.isdir():
                entry = {"kind": "directory", "mode": 0o755}
            elif member.issym():
                entry = {"kind": "symlink", "target": member.linkname}
            elif member.isfile() and not member.sparse:
                stream = source.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable archive member: {name}")
                with stream:
                    entry = {"kind": "file", "size": member.size,
                             "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                             "mode": 0o644 | (member.mode & 0o111)}
            else:
                raise ValueError(f"unsupported archive member: {name}")
            entries[name] = entry
            if len(entries) > MAX_MEMBERS:
                raise ValueError("too many package members")
        # Read through gzip's trailer/CRC, not only tar's first end marker.
        compressed.seek(source.offset)
        tail_size = 0
        for chunk in iter(lambda: compressed.read(1024 * 1024), b""):
            tail_size += len(chunk)
            if chunk.strip(b"\0"):
                raise ValueError("unexpected data after tar members")
        if tail_size < 1024:
            raise ValueError("missing tar end markers")
    if not entries:
        raise ValueError("empty transfer archive")
    roots = {name.split("/")[0] for name in entries}
    if len(roots) != 1 or entries.get(next(iter(roots)), {}).get("kind") != "directory":
        raise ValueError("archive must have one directory root")
    validate_links(entries)
    if digest(archive) != actual_sha:
        raise ValueError("archive changed during verification")
    return {"format": 1, "sha256": actual_sha, "members": len(entries),
            "files_sha256": index_digest(entries), "entries": entries}


def fresh_output(output: Path) -> Path:
    output = output.absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("output path must not contain symlinks")
    if output.exists():
        raise ValueError("output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def create(root: Path, output: Path, prefix: str | None = None) -> dict:
    root = root.absolute()
    prefix = canonical(prefix or root.name)
    if "/" in prefix or metadata_path(prefix):
        raise ValueError("invalid package root name")
    if output.absolute().is_relative_to(root.resolve()):
        raise ValueError("archive output overlaps source tree")
    entries, excluded = scan(root, filter_metadata=True)
    output = fresh_output(output)
    expected = {prefix: {"kind": "directory", "mode": 0o755},
                **{f"{prefix}/{name}": entry for name, entry in entries.items()}}
    with tempfile.TemporaryDirectory(prefix=".tsp-pack-", dir=output.parent) as temp:
        archive = Path(temp) / "payload.tar.gz"
        with archive.open("xb") as raw, gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0,
        ) as compressed, tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as target:
            for name, entry in expected.items():
                member = tarfile.TarInfo(name)
                member.mode = entry.get("mode", 0o777)
                if entry["kind"] == "directory":
                    member.type = tarfile.DIRTYPE
                elif entry["kind"] == "symlink":
                    member.type, member.linkname = tarfile.SYMTYPE, entry["target"]
                else:
                    member.size = entry["size"]
                    path = root / name.removeprefix(prefix + "/")
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(descriptor, "rb") as stream:
                        target.addfile(member, stream)
                    continue
                target.addfile(member)
        report = verify(archive)
        if report["entries"] != expected or scan(root, filter_metadata=True)[0] != entries:
            raise ValueError("source changed while packaging")
        # Hard-link publication fails atomically if another output appeared.
        os.link(archive, output)
    return {**report, "excluded": excluded}


def verify_tree(root: Path, archive: Path | None = None, sha256: str | None = None) -> dict:
    entries, _ = scan(root)
    result = {"format": 1, "members": len(entries), "files_sha256": index_digest(entries)}
    if archive is not None:
        expected = verify(archive, sha256)
        if entries != expected["entries"]:
            raise ValueError("extracted tree differs from verified archive")
        result["sha256"] = expected["sha256"]
    return result


def extract(archive: Path, output: Path, sha256: str) -> dict:
    report = verify(archive, sha256)
    output = fresh_output(output)
    with tempfile.TemporaryDirectory(prefix=".tsp-unpack-", dir=output.parent) as temp:
        staging = Path(temp) / "tree"
        staging.mkdir()
        with tarfile.open(archive, "r:gz") as source:
            for name, entry in report["entries"].items():
                target = staging / name
                if entry["kind"] == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(entry["mode"])
                elif entry["kind"] == "file":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = source.extractfile(name)
                    if stream is None:
                        raise ValueError("archive changed during extraction")
                    with stream, target.open("xb") as destination:
                        for chunk in iter(lambda stream=stream: stream.read(1024 * 1024), b""):
                            destination.write(chunk)
                    target.chmod(entry["mode"])
            # Links are created last; no file writes ever follow a package link.
            for name, entry in report["entries"].items():
                if entry["kind"] == "symlink":
                    (staging / name).symlink_to(entry["target"])
        verify_tree(staging, archive, sha256)
        if output.exists() or output.is_symlink():
            raise ValueError("output appeared while extracting")
        staging.rename(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify", "extract", "verify-tree"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prefix")
    parser.add_argument("--sha256")
    args = parser.parse_args()
    try:
        if args.action == "create" and args.root and args.output:
            report = create(args.root, args.output, args.prefix)
        elif args.action == "verify" and args.archive:
            report = verify(args.archive, args.sha256)
        elif args.action == "extract" and args.archive and args.output and args.sha256:
            report = extract(args.archive, args.output, args.sha256)
        elif args.action == "verify-tree" and args.root:
            report = verify_tree(args.root, args.archive, args.sha256)
        else:
            raise ValueError("missing required arguments; extract requires --sha256")
    except (ValueError, OSError, EOFError, tarfile.TarError) as exc:
        parser.exit(1, f"package transfer failed: {exc}\n")
    print(json.dumps({k: v for k, v in report.items() if k != "entries"},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
