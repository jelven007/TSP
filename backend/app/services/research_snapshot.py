"""Retained local research inputs, isolated from subsequent market writes."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    _try_lock_file,
    _unlock_file,
    enriched_publication_incomplete,
)
from app.market_time import trading_calendar_provenance
from app.multitenancy.publication import PUBLIC_DIRS, PUBLIC_FILES
from app.services.data_release import _atomic_write_json, stable_hash

MAX_FILES = 100_000
MAX_BYTES = 50 * 1024**3
MAX_VERSIONS = 20
MAX_RETAINED_BYTES = 100 * 1024**3
# These workers do not consume live depth, auction or second-replay inputs.
# Including them would let unrelated per-second writes starve a historical run.
RESEARCH_DIRS = PUBLIC_DIRS - {
    "depth5", "auction_snapshot", "quote_second", "market_snapshot", "analysis_menus", "corporate_actions",
}
PRIVATE_DIRS = frozenset({
    "strategies/custom", "strategies/ai", "strategies/composite",
    "user_data/strategy_overrides", "user_data/custom_factors", "user_data/custom_signals",
})
MARKER = ".research_snapshot.json"


class ResearchSnapshotError(RuntimeError):
    """Inputs cannot be frozen or verified; never continue on mutable data."""


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ResearchSnapshotError("research input capture cancelled")


def _inventory(root: Path, *, max_files=MAX_FILES, max_bytes=MAX_BYTES) -> dict:
    files, total = {}, 0
    for directory in sorted(RESEARCH_DIRS | PRIVATE_DIRS):
        base = root / directory
        if any(parent.is_symlink() for parent in [base, *base.parents] if parent != root.parent):
            raise ResearchSnapshotError("symbolic research input is not supported")
        if not base.exists():
            continue
        if not base.is_dir():
            raise ResearchSnapshotError("research input directory is invalid")
        def unreadable(error):
            raise ResearchSnapshotError("research input directory is unreadable") from error

        for parent, dirs, names in os.walk(base, followlinks=False, onerror=unreadable):
            for name in [*dirs, *names]:
                path = Path(parent) / name
                if path.is_symlink():
                    raise ResearchSnapshotError("symbolic research input is not supported")
            for name in names:
                path = Path(parent) / name
                if path.suffix in {".parquet", ".json"} or (
                    directory in PRIVATE_DIRS and path.suffix == ".py"
                ):
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        raise ResearchSnapshotError("research input must be a regular file")
                    total += info.st_size
                    files[path.relative_to(root).as_posix()] = (
                        info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                    )
                    if len(files) > max_files or total > max_bytes:
                        raise ResearchSnapshotError("research input file/byte limit exceeded")
    for name in sorted(PUBLIC_FILES):
        path = root / name
        if path.is_symlink():
            raise ResearchSnapshotError("symbolic research input is not supported")
        if path.is_file():
            info = path.stat()
            total += info.st_size
            files[name] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if len(files) > max_files or total > max_bytes:
        raise ResearchSnapshotError("research input file/byte limit exceeded")
    return dict(sorted(files.items()))


def _sanitized(path: Path) -> bytes | None:
    if path.name == "config.json" and path.parent.parent.name == "ext_data":
        if path.stat().st_size > 4 * 1024**2:
            raise ResearchSnapshotError("extended configuration size limit exceeded")
        config = json.loads(path.read_text(encoding="utf-8"))
        config.pop("pull", None)
        return json.dumps(config, ensure_ascii=False, sort_keys=True).encode()
    return None


def _file_hash(path, cancel=None):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            _check_cancel(cancel)
            size += len(block)
            if size > MAX_BYTES:
                raise ResearchSnapshotError("research input byte limit exceeded")
            digest.update(block)
    return {"sha256": digest.hexdigest(), "bytes": size}


def _copy_file(source, target, cancel):
    size = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        while block := reader.read(1024**2):
            _check_cancel(cancel)
            size += len(block)
            if size > MAX_BYTES:
                raise ResearchSnapshotError("research input byte limit exceeded")
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())


def _assert_stable(root, before, cancel):
    _check_cancel(cancel)
    if _inventory(root) != before or any(
        enriched_publication_incomplete(root, asset) for asset in ("stock", "etf", "index")
    ):
        raise ResearchSnapshotError("research inputs changed during capture; retry")


def _runtime_identity(cancel=None):
    app_root = Path(__file__).resolve().parents[1]
    sources = {
        path.relative_to(app_root).as_posix(): _file_hash(path, cancel)
        for path in sorted(app_root.rglob("*.py"))
    }
    if getattr(sys, "frozen", False):
        packaged_manifest = Path(sys._MEIPASS) / "source-manifest.json"
        if not packaged_manifest.is_file():
            raise ResearchSnapshotError("packaged research build manifest is unavailable")
        sources["packaged_manifest"] = _file_hash(packaged_manifest, cancel)
        sources["packaged_executable"] = _file_hash(Path(sys.executable), cancel)
    return {
        "app_sources_sha256": stable_hash(sources),
        "python": sys.version.split()[0],
        "packages": {name: version(name) for name in ("polars", "numpy", "duckdb", "pyarrow")},
    }


def verify_research_snapshot(root: Path, *, cancel=None) -> dict:
    root = Path(root)
    if root.is_symlink() or (root / MARKER).is_symlink():
        raise ResearchSnapshotError("symbolic research snapshot is not supported")
    try:
        manifest = json.loads((root / MARKER).read_text(encoding="utf-8"))
        files = manifest["files"]
        identity = {key: manifest[key] for key in ("files", "trading_calendar", "runtime")}
        if (manifest.get("schema_version") != 1 or manifest["id"] != stable_hash(identity)
                or manifest["id"] != root.name or set(_inventory(root)) != set(files)):
            raise ResearchSnapshotError("research snapshot inventory/checksum mismatch")
        if manifest["trading_calendar"] != trading_calendar_provenance():
            raise ResearchSnapshotError("research snapshot trading calendar changed")
        if manifest["runtime"] != _runtime_identity(cancel):
            raise ResearchSnapshotError("research snapshot runtime changed; use its recorded build")
        for name, expected in files.items():
            if _file_hash(root / name, cancel) != expected:
                raise ResearchSnapshotError("research snapshot checksum mismatch")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ResearchSnapshotError("research snapshot is unreadable") from exc


def snapshot_summary(manifest: dict) -> dict:
    return {
        "id": manifest["id"], "file_count": len(manifest["files"]),
        "bytes": sum(item["bytes"] for item in manifest["files"].values()),
        "created_at": manifest["created_at"], "scope": "all_retained_history",
        "runtime": manifest["runtime"],
    }


def _check_retention(destination, new_bytes, max_versions):
    retained = [path for path in destination.iterdir() if not path.name.startswith(".")]
    if len(retained) >= max_versions:
        raise ResearchSnapshotError("retained research version limit reached; archive unused versions")
    total = new_bytes
    for path in retained:
        marker = path / MARKER
        if path.is_symlink() or marker.is_symlink() or marker.stat().st_size > 64 * 1024**2:
            raise ResearchSnapshotError("retained research version is invalid")
        manifest = json.loads(marker.read_text(encoding="utf-8"))
        total += sum(item["bytes"] for item in manifest["files"].values())
    if total > MAX_RETAINED_BYTES:
        raise ResearchSnapshotError("retained research byte limit reached; archive unused versions")


def capture_research_snapshot(
    source: Path, *, cancel=None, max_files=MAX_FILES, max_bytes=MAX_BYTES,
    max_versions=MAX_VERSIONS, snapshot_id: str | None = None,
) -> tuple[Path, dict]:
    """Capture once per complete task; retain full warmup/exit/fold history."""
    source = Path(source).resolve()
    destination = source / "research_inputs"
    if destination.is_symlink():
        raise ResearchSnapshotError("symbolic snapshot directory is not supported")
    _check_cancel(cancel)
    if snapshot_id is not None:
        if len(snapshot_id) != 64 or any(c not in "0123456789abcdef" for c in snapshot_id):
            raise ResearchSnapshotError("invalid research snapshot id")
        root = destination / snapshot_id
        return root, verify_research_snapshot(root, cancel=cancel)
    before = _inventory(source, max_files=max_files, max_bytes=max_bytes)
    _assert_stable(source, before, cancel)
    files = {}
    for name in before:
        _check_cancel(cancel)
        sanitized = _sanitized(source / name)
        files[name] = (
            {"sha256": hashlib.sha256(sanitized).hexdigest(), "bytes": len(sanitized)}
            if sanitized is not None else _file_hash(source / name, cancel)
        )
    generated = {}
    # Legacy local datasets may have no generation marker. Give only the retained
    # view a content-derived generation; this does not certify a published release.
    for asset in ("stock", "etf", "index"):
        name = f".matrix_generation_{asset}.json"
        if name not in files:
            payload = json.dumps({
                "state": "ready", "generation": stable_hash({"asset": asset, "files": files}),
            }, sort_keys=True).encode()
            generated[name] = payload
    for name, payload in generated.items():
        files[name] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    if len(files) > max_files or sum(item["bytes"] for item in files.values()) > max_bytes:
        raise ResearchSnapshotError("research input file/byte limit exceeded")
    identity = {
        "files": files, "trading_calendar": trading_calendar_provenance(),
        "runtime": _runtime_identity(cancel),
    }
    snapshot_id = stable_hash(identity)
    root = destination / snapshot_id
    _assert_stable(source, before, cancel)
    if root.exists():
        return root, verify_research_snapshot(root, cancel=cancel)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    new_bytes = sum(item["bytes"] for item in files.values())
    _check_retention(destination, new_bytes, max_versions)
    stage = destination / f".{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    try:
        for name in files:
            _check_cancel(cancel)
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            sanitized = generated.get(name) if name in generated else _sanitized(source / name)
            if sanitized is None:
                _copy_file(source / name, target, cancel)
            else:
                target.write_bytes(sanitized)
            if _file_hash(target, cancel) != files[name]:
                raise ResearchSnapshotError("research inputs changed while copying; retry")
        _assert_stable(source, before, cancel)
        if identity["runtime"] != _runtime_identity(cancel):
            raise ResearchSnapshotError("research runtime changed during capture; retry")
        manifest = {
            "schema_version": 1, "id": snapshot_id, **identity,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write_json(stage / MARKER, manifest)
        lock_path = destination / ".commit.lock"
        if lock_path.is_symlink():
            raise ResearchSnapshotError("symbolic snapshot lock is not supported")
        with lock_path.open("a+b") as lock:
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            try:
                _try_lock_file(lock)
            except EnrichedGenerationUnavailableError as exc:
                raise ResearchSnapshotError("research snapshot commit busy; retry") from exc
            try:
                if root.exists():
                    return root, verify_research_snapshot(root, cancel=cancel)
                _check_retention(destination, new_bytes, max_versions)
                stage.rename(root)
            finally:
                _unlock_file(lock)
        return root, manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)
