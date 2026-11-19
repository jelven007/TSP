#!/usr/bin/env python3
"""Fast-forward a workspace while preserving staged and unstaged work separately."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


def git(root, *args, data=None, env=None):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], input=data, env=env,
    )


def tree(root, revision, paths=()):
    result = {}
    for record in git(root, "ls-tree", "-rz", revision, "--", *paths).split(b"\0"):
        if record:
            header, path = record.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            if kind != "blob":
                raise ValueError("Submodules require separate workspace synchronization")
            result[path.decode()] = mode, oid
    return result


def index_tree(root):
    result = {}
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if record:
            header, path = record.split(b"\t", 1)
            mode, oid, stage = header.decode().split()
            if stage != "0":
                raise ValueError("Resolve the existing index conflicts first")
            result[path.decode()] = mode, oid
    return result


def read_work(path):
    if path.is_symlink():
        return "120000", os.readlink(path).encode()
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Expected a file: {path}")
    return ("100755" if path.stat().st_mode & 0o111 else "100644"), path.read_bytes()


def materialize(root, mappings):
    ids = sorted({oid for mapping in mappings for _, oid in mapping.values()})
    raw = git(root, "cat-file", "--batch", data=("\n".join(ids) + "\n").encode())
    stream, blobs = io.BytesIO(raw), {}
    for oid in ids:
        header = stream.readline().decode().split()
        if len(header) != 3 or header[1] != "blob":
            raise ValueError(f"Cannot read source object {oid}")
        blobs[oid] = stream.read(int(header[2]))
        if stream.read(1) != b"\n":
            raise ValueError("Invalid git object stream")
    return [{path: (mode, blobs[oid]) for path, (mode, oid) in mapping.items()}
            for mapping in mappings]


def merge_value(base, local, target):
    if local == base:
        return target, False
    if target == base or local == target:
        return local, False
    # Preserve deliberate local deletion, including previously staged additions.
    if local is None:
        return None, False
    if base is None or target is None:
        return local, True
    if base[0] != local[0] or base[0] != target[0] or base[0] == "120000":
        return local, True
    with tempfile.TemporaryDirectory(prefix="tsp-workspace-merge-") as directory:
        paths = [Path(directory) / name for name in ("local", "base", "target")]
        for path, value in zip(paths, (local, base, target), strict=True):
            path.write_bytes(value[1])
        completed = subprocess.run(
            ["git", "merge-file", "-p", *map(str, paths)], capture_output=True,
        )
    return (local[0], completed.stdout), completed.returncode != 0


def signature(values):
    return {path: None if value is None else {
        "mode": value[0], "sha256": hashlib.sha256(value[1]).hexdigest(),
    } for path, value in values.items()}


def write_work(destination, value):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        destination.unlink()
    if value is None:
        destination.unlink(missing_ok=True)
    elif value[0] == "120000":
        destination.unlink(missing_ok=True)
        destination.symlink_to(value[1].decode())
    else:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                temporary.write(value[1])
                temporary_path = Path(temporary.name)
            temporary_path.chmod(int(value[0][-3:], 8))
            temporary_path.replace(destination)
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)


def sync_workspace(root, target, protection, *, apply=False, preserve_conflicts=False):
    root, protection = Path(root).resolve(), Path(protection).resolve()
    head = git(root, "rev-parse", "HEAD").decode().strip()
    target = git(root, "rev-parse", f"{target}^{{commit}}").decode().strip()
    git(root, "merge-base", "--is-ancestor", head, target)
    raw_index = index_tree(root)
    base, staged, upstream = materialize(root, [tree(root, head), raw_index, tree(root, target)])
    paths = sorted(set(base) | set(staged) | set(upstream))
    working = {path: read_work(root / path) for path in paths}
    planned_index, planned_work, conflicts = {}, {}, []
    layer_conflicts = {"index": [], "work": []}
    for path in paths:
        planned_index[path], index_conflict = merge_value(
            base.get(path), staged.get(path), upstream.get(path),
        )
        if path not in base and path not in staged:
            # New upstream files must not replace untracked or ignored user files.
            planned_work[path] = upstream.get(path) if working[path] is None else working[path]
            work_conflict = working[path] is not None and working[path] != upstream.get(path)
        else:
            planned_work[path], work_conflict = merge_value(
                base.get(path), working[path], upstream.get(path),
            )
        if index_conflict or work_conflict:
            conflicts.append(path)
        if index_conflict:
            layer_conflicts["index"].append(path)
        if work_conflict:
            layer_conflicts["work"].append(path)
        if preserve_conflicts:
            if index_conflict:
                planned_index[path] = staged.get(path)
            if work_conflict:
                planned_work[path] = working[path]
    report = {
        "status": "conflict" if conflicts else "ready", "from_revision": head,
        "revision": target, "conflicts": conflicts,
        "layer_conflicts": layer_conflicts,
        "changed_files": sum(working[path] != planned_work[path] for path in paths),
    }
    if not apply or (conflicts and not preserve_conflicts):
        return report
    if protection == root or root in protection.parents:
        raise ValueError("Protection must be outside the workspace")
    if protection.exists():
        raise ValueError("Use a new protection directory")
    protection.mkdir(parents=True, mode=0o700)
    index_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index")
                      .decode().strip())
    original_index = index_path.read_bytes()
    (protection / "index").write_bytes(original_index)
    with tarfile.open(protection / "work.tar", "w") as archive:
        for layer, values in (("index", staged), ("work", working)):
            for path, value in values.items():
                if value is None or value == base.get(path) or value == upstream.get(path):
                    continue
                entry = tarfile.TarInfo(f"{layer}/{path}")
                entry.size, entry.mode = len(value[1]), int(value[0][-3:], 8)
                archive.addfile(entry, io.BytesIO(value[1]))
    with tarfile.open(protection / "work.tar") as archive:
        for entry in archive:
            layer, path = entry.name.split("/", 1)
            expected = (staged if layer == "index" else working)[path][1]
            if archive.extractfile(entry).read() != expected:
                raise ValueError("Work protection verification failed")
    report["protected_files"] = signature(working)
    (protection / "before.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    shadow_index = protection / "new-index"
    environment = dict(os.environ, GIT_INDEX_FILE=str(shadow_index))
    git(root, "read-tree", target, env=environment)
    changes = []
    for path, value in planned_index.items():
        if value == upstream.get(path):
            continue
        if value is None:
            changes.append(f"0 {'0' * 40}\t{path}\0".encode())
        else:
            oid = git(root, "hash-object", "-w", "--stdin", data=value[1]).decode().strip()
            changes.append(f"{value[0]} {oid}\t{path}\0".encode())
    git(root, "update-index", "-z", "--index-info", data=b"".join(changes), env=environment)
    lock = Path(str(index_path) + ".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    touched = []
    head_updated = False
    try:
        if (git(root, "rev-parse", "HEAD").decode().strip() != head
                or index_path.read_bytes() != original_index
                or signature({path: read_work(root / path) for path in paths}) != signature(working)):
            raise ValueError("Concurrent workspace modification; synchronization stopped")
        for path in paths:
            value, before = planned_work[path], working[path]
            if value == before:
                continue
            touched.append(path)
            write_work(root / path, value)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(shadow_index.read_bytes())
        descriptor = None
        git(root, "update-ref", "-m", "Synchronize released workspace preserving development",
            "HEAD", target, head)
        head_updated = True
        lock.replace(index_path)
        if (git(root, "rev-parse", "HEAD").decode().strip() != target
                or signature({path: read_work(root / path) for path in paths}) != signature(planned_work)):
            raise ValueError("Workspace synchronization postcheck failed")
    except Exception:
        # Restore only our writes; a concurrent editor's newer content stays intact.
        for path in reversed(touched):
            current = read_work(root / path)
            if current == planned_work[path] or current is None:
                write_work(root / path, working[path])
        if head_updated:
            git(root, "update-ref", "-m", "Roll back failed workspace synchronization",
                "HEAD", head, target)
        if index_path.read_bytes() != original_index:
            lock.write_bytes(original_index)
            lock.replace(index_path)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock.unlink(missing_ok=True)
    shutil.copyfile(shadow_index, protection / "after-index")
    report.pop("protected_files")
    report["status"] = "preserved" if conflicts else "passed"
    (protection / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--protection", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preserve-conflicts", action="store_true",
                        help="Keep conflicting local files verbatim; does not certify full synchronization")
    args = parser.parse_args()
    report = sync_workspace(args.workspace, args.revision, args.protection, apply=args.apply,
                            preserve_conflicts=args.preserve_conflicts)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["status"] == "conflict")


if __name__ == "__main__":
    raise SystemExit(main())
