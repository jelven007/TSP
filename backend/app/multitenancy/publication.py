"""Immutable, credential-free public files for APIs and compute containers."""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from app.services.observability import measure_runtime_lock

logger = logging.getLogger(__name__)
PUBLIC_DIRS = frozenset({
    "kline_daily", "kline_daily_enriched", "kline_index_daily", "kline_index_enriched",
    "kline_index_weekly", "kline_index_monthly",
    "kline_minute",
    "kline_5minute", "kline_10minute", "kline_15minute", "kline_weekly",
    "adj_factor", "financials", "instruments", "instruments_index",
    "instruments_ext", "kline_ext", "depth5", "auction_snapshot",
    "quote_second", "regime_history", "mainline_history", "ext_data", "analysis_menus",
    "instrument_status", "data_releases",
    "market_snapshot", "corporate_actions",
})
PUBLIC_FILES = frozenset({
    ".matrix_generation_stock.json", ".matrix_generation_index.json",
    "settlement_ready.json", "capabilities.json",
})
DAILY_COVERAGE_DIRS = (
    ("stock", "kline_daily", "kline_daily_enriched"),
    ("index", "kline_index_daily", "kline_index_enriched"),
)


def _strip_volatile(config: dict) -> None:
    """Drop ext config fields the scheduler rewrites on every pull tick."""
    config.pop("pull", None)
    config.pop("updated_at", None)


def release_path(root: Path, release_id: str | None = None) -> Path:
    root = root.resolve()
    if release_id is not None:
        if len(release_id) != 32 or any(char not in "0123456789abcdef" for char in release_id):
            raise ValueError("invalid public release")
        target = root / "releases" / release_id
    else:
        target = root / "current" if (root / "current").exists() else root
    target = target.resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_dir():
        raise ValueError("public release is unavailable")
    return target


class PublicPublisher:
    def __init__(self, source, destination, write_lock=None):
        self.source, self.root = Path(source).resolve(), Path(destination).resolve()
        if self.source == self.root or self.root.is_relative_to(self.source):
            raise ValueError("public releases must be outside the source data directory")
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="public-file-publisher")
        self.error = None
        self.write_lock = write_lock or threading.Lock()

    def generations(self):
        from app.enriched_generation import get_enriched_generation

        return {asset: get_enriched_generation(self.source, asset, initialize=False)
                for asset in ("stock", "index")}

    def inventory(self):
        files = {}
        for directory in PUBLIC_DIRS:
            for path in (self.source / directory).rglob("*"):
                if path.is_symlink() or not path.is_file() or path.suffix not in {".parquet", ".json"}:
                    continue
                files[path.relative_to(self.source).as_posix()] = self._signature(path)
        for name in PUBLIC_FILES:
            path = self.source / name
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                files[name] = [stat.st_size, stat.st_mtime_ns]
        return files

    @staticmethod
    def _signature(path: Path):
        if path.parent.parent.name == "ext_data" and path.name == "config.json":
            # The ext scheduler rewrites pull progress and updated_at into
            # config.json every minute. publish() strips those blocks, so the
            # volatile mtime must not invalidate a release still copying files.
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            else:
                _strip_volatile(config)
                digest = hashlib.sha256(
                    json.dumps(config, sort_keys=True, ensure_ascii=False).encode(),
                ).hexdigest()
                return [0, int(digest[:15], 16)]
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def publish(self):
        # Serialize the short copy boundary with the platform repository's
        # writers. Otherwise a one-second quote cycle could starve publication.
        with measure_runtime_lock(self.write_lock, "public.publish"):
            return self._publish()

    def validate_daily_coverage(self):
        import polars as pl

        for asset, raw_name, enriched_name in DAILY_COVERAGE_DIRS:
            raw_root = self.source / raw_name
            raw_partition = max(
                (path for path in raw_root.glob("date=????-??-??") if path.is_dir()),
                default=None,
            )
            if raw_partition is None:
                continue
            enriched_partition = self.source / enriched_name / raw_partition.name
            raw_files = sorted(raw_partition.rglob("*.parquet"))
            enriched_files = (
                sorted(enriched_partition.rglob("*.parquet"))
                if enriched_partition.is_dir() else []
            )
            if not raw_files or not enriched_files:
                raise ValueError(
                    f"public enriched coverage missing for {asset} {raw_partition.name}",
                )
            try:
                raw_symbols = set(
                    pl.read_parquet(raw_files, columns=["symbol"])["symbol"].drop_nulls().to_list(),
                )
                enriched_symbols = set(
                    pl.read_parquet(enriched_files, columns=["symbol"])["symbol"].drop_nulls().to_list(),
                )
            except Exception as exc:
                raise ValueError(
                    f"public enriched coverage unreadable for {asset} {raw_partition.name}",
                ) from exc
            missing = raw_symbols - enriched_symbols
            if missing:
                sample = ",".join(sorted(missing)[:5])
                raise ValueError(
                    f"public enriched coverage incomplete for {asset} "
                    f"{raw_partition.name}: {len(missing)} missing ({sample})",
                )

    def _publish(self):
        self.validate_daily_coverage()
        generations, files = self.generations(), self.inventory()
        digest = hashlib.sha256(json.dumps([generations, files], sort_keys=True).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        previous = None
        try:
            previous = release_path(self.root)
            previous_manifest = json.loads((previous / ".public_release.json").read_text())
        except (OSError, ValueError):
            previous_manifest = {}
        if previous_manifest.get("digest") == digest and "checksums" in previous_manifest:
            return previous_manifest
        if hasattr(os, "statvfs"):
            capacity = os.statvfs(self.root)
            # A release needs fresh directory inodes even for hard-linked files.
            # Budget every file as a copy, retaining 10%/100k inodes and 1 GiB
            # for the source writer and the rest of the host.
            directories = {
                parent.as_posix()
                for name in files
                for parent in Path(name).parents
                if parent != Path(".")
            }
            needed = len(files) + len(directories) + 3
            reserve = max(100_000, capacity.f_files // 10)
            bytes_needed = sum(
                signature[0] for name, signature in files.items()
                if previous_manifest.get("files", {}).get(name) != signature
            )
            if (
                (capacity.f_files and capacity.f_ffree - needed < reserve)
                or capacity.f_bavail * capacity.f_frsize - bytes_needed < 1024**3
            ):
                raise OSError(errno.ENOSPC, "public release capacity reserve")
        release_id = uuid.uuid4().hex
        releases = self.root / "releases"
        releases.mkdir(exist_ok=True)
        stage = releases / ("." + release_id)
        stage.mkdir()
        try:
            checksums = {}
            for name, signature in files.items():
                source, target = self.source / name, stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if previous and previous_manifest.get("files", {}).get(name) == signature:
                    # Only link a previous immutable COPY, never mutable source data.
                    os.link(previous / name, target)
                elif name.startswith("ext_data/") and name.endswith("/config.json"):
                    config = json.loads(source.read_text(encoding="utf-8"))
                    _strip_volatile(config)
                    target.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                    os.utime(target, ns=(signature[1], signature[1]))
                else:
                    shutil.copy2(source, target)
                retained_hash = previous_manifest.get("checksums", {}).get(name)
                if retained_hash and previous_manifest.get("files", {}).get(name) == signature:
                    checksums[name] = retained_hash
                else:
                    with target.open("rb") as stream:
                        checksums[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            if self.generations() != generations or self.inventory() != files:
                raise ValueError("public files changed during publication; retry")
            manifest = {"id": release_id, "digest": digest, "generations": generations,
                        "files": files, "checksums": checksums, "published_at": time.time()}
            (stage / ".public_release.json").write_text(json.dumps(manifest, sort_keys=True))
            os.rename(stage, releases / release_id)
            link = self.root / ("." + release_id)
            link.symlink_to(Path("releases") / release_id, target_is_directory=True)
            self._swap_current(link)
            self.error = None
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    @staticmethod
    def _swap_current(link):
        current = link.parent / "current"
        try:
            os.replace(link, current)
        except OSError:
            # Windows MoveFileEx cannot swap a directory entry for a link, so the
            # stale link is dropped first. POSIX keeps the atomic rename; only the
            # publisher's own link is removed, never a real directory holding data.
            if os.name != "nt" or not current.is_symlink():
                raise
            current.unlink()
            os.replace(link, current)

    def start(self):
        # The first complete public release is a deployment readiness gate.
        try:
            self.publish()
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            previous = release_path(self.root)
            manifest = json.loads((previous / ".public_release.json").read_text())
            if manifest.get("id") != previous.name or not manifest.get("checksums"):
                raise
            self.error = type(exc).__name__
            logger.warning("public file publication deferred: %s", self.error)
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=20)

    def _loop(self):
        # File releases feed immutable research inputs; live quotes/depth use the
        # independent market bridge. Frequent copies duplicate every directory
        # inode even when unchanged files are hard-linked to the prior release.
        delay = 300
        while not self.stopped.wait(delay):
            try:
                self.publish()
                delay = 300
            except Exception as exc:
                self.error = type(exc).__name__
                logger.warning("public file publication deferred: %s", self.error)
                delay = 300 if isinstance(exc, OSError) and exc.errno == errno.ENOSPC else 30
