"""Immutable, credential-free public files for APIs and compute containers."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)
PUBLIC_DIRS = frozenset({
    "kline_daily", "kline_daily_enriched", "kline_index_daily", "kline_index_enriched",
    "kline_etf_daily", "kline_etf_enriched", "kline_minute", "kline_etf_minute",
    "adj_factor", "adj_factor_etf", "financials", "instruments", "instruments_index",
    "instruments_etf", "instruments_ext", "kline_ext", "depth5", "auction_snapshot",
    "quote_second", "regime_history", "mainline_history", "ext_data", "analysis_menus",
    "instrument_status", "data_releases",
    "market_snapshot", "corporate_actions",
})
PUBLIC_FILES = frozenset({
    ".matrix_generation_stock.json", ".matrix_generation_etf.json", ".matrix_generation_index.json",
    "settlement_ready.json", "capabilities.json",
})


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
                for asset in ("stock", "etf", "index")}

    def inventory(self):
        files = {}
        for directory in PUBLIC_DIRS:
            for path in (self.source / directory).rglob("*"):
                if path.is_symlink() or not path.is_file() or path.suffix not in {".parquet", ".json"}:
                    continue
                stat = path.stat()
                files[path.relative_to(self.source).as_posix()] = [stat.st_size, stat.st_mtime_ns]
        for name in PUBLIC_FILES:
            path = self.source / name
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                files[name] = [stat.st_size, stat.st_mtime_ns]
        return files

    def publish(self):
        # Serialize the short copy boundary with the platform repository's
        # writers. Otherwise a one-second quote cycle could starve publication.
        with self.write_lock:
            return self._publish()

    def _publish(self):
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
                    config = json.loads(source.read_text())
                    config.pop("pull", None)
                    target.write_text(json.dumps(config, ensure_ascii=False))
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
        self.publish()
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=20)

    def _loop(self):
        while not self.stopped.wait(30):
            try:
                self.publish()
            except Exception as exc:
                self.error = type(exc).__name__
                logger.warning("public file publication deferred: %s", self.error)
