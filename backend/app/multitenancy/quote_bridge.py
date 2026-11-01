"""A single public market reader per full API process; original read/SSE contract."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.multitenancy.market import decode_frame
from app.multitenancy.public_reader import (
    RELEASE_ID,
    PublicReleaseError,
    attach_public_lease,
    current_release,
    open_public_repository,
)
from app.services.depth_service import DepthService
from app.services.observability import measure_runtime_lock, measure_runtime_stage
from app.services.quote_service import QuoteService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicBindingView:
    repo: object
    snapshot: dict | None
    lease: object
    received_at: float
    indicators_ready: bool
    sync: dict


@dataclass
class PreparedPublicBinding:
    repo: object
    snapshot: dict
    frames: dict
    indices: object
    depth: object
    sector: object
    new_repository: bool
    committed: bool = False

    def close(self):
        if self.new_repository and not self.committed:
            self.repo.store._public_release_finalizer()


class PublicQuoteBridge(QuoteService):
    def __init__(self, repo, market, state=None):
        super().__init__()
        self.set_repo(repo)
        self.market = market
        self.state = state
        self.depth = PublicDepthBridge(repo)
        self.snapshot = None
        self.received_at = 0
        self.update_lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.markers = None
        self.update_generation = 0
        self.loaded_generation = -1
        self.snapshot_fetches = 0
        self.frame_decodes = 0
        self._closed = threading.Event()
        self._started = False
        if RELEASE_ID.fullmatch(repo.store.data_dir.name) and repo.store.data_dir.parent.name == "releases":
            attach_public_lease(repo.store)
        from app.multitenancy.public_binding_sync import PublicBindingSynchronizer

        self.binding_sync = PublicBindingSynchronizer(self)

    def start(self):
        with self.update_lock:
            if self._started or self._closed.is_set():
                return
            self._started = True
        if self.markers is not None:
            self.markers.start()
        if self.snapshot and self.snapshot.get("public_release"):
            self.binding_sync.start()

    def attach_markers(self, markers):
        self.markers = markers

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        # Signal first, then close transports to unblock I/O, then join. Live
        # request/thread references continue owning their stores on timeout.
        self.binding_sync.close(timeout=0)
        if self.markers is not None:
            self.markers.close()
        self.market.close()
        self.binding_sync.close()
        with self.update_lock:
            repo = self._repo
            if self.state is not None:
                for name in ("repo", "datastore", "sector_monitor_service"):
                    value = getattr(self.state, name, None)
                    if value is repo or value is getattr(repo, "store", None) or getattr(value, "_repo", None) is repo:
                        setattr(self.state, name, None)
            self._repo = None
            self.depth.set_repo(None)
            self.snapshot = None

    def bound_release(self):
        with self.update_lock:
            return self._repo.store.data_dir if self._repo is not None else None

    def binding_snapshot(self):
        with self.update_lock:
            repo = self._repo
            return PublicBindingView(
                repo, self.snapshot, getattr(getattr(repo, "store", None), "_public_release_lease", None),
                self.received_at, bool(getattr(self.state, "indicators_ready", False)),
                self.binding_sync.snapshot(),
            )

    def status(self):
        if self.snapshot is None:
            return {
                "enabled": False, "running": False, "symbol_count": 0, "index_symbol_count": 0,
                "quote_age_ms": None, "is_trading_hours": False, "last_fetch_ms": None,
            }
        value = dict(self.snapshot["status"])
        elapsed = (time.monotonic() - self.received_at) * 1000
        if value.get("quote_age_ms") is not None:
            value["quote_age_ms"] += elapsed
        if elapsed > 15000:
            value.update(running=False, is_trading_hours=False)
        return value

    def refresh(self, *, force=True, max_age=5):
        with measure_runtime_lock(self.refresh_lock, "public.refresh"):
            marker_connected = bool(self.markers and self.markers.status()["connected"])
            stale = not self.snapshot or self.loaded_generation < self.update_generation
            fallback_stale = (
                time.monotonic() - self.received_at >= max_age and (
                    not marker_connected
                    or Path(settings.data_dir).resolve() != self._repo.store.data_dir.resolve()
                )
            )
            if not force and not stale and not fallback_stale:
                return self.status()
            with self.update_lock:
                generation = self.update_generation
            snapshot = self._fetch_snapshot()
            self._update(snapshot, generation=generation)
            return self.status()

    def ensure_fresh(self, max_age=5):
        marker_connected = bool(self.markers and self.markers.status()["connected"])
        stale = not self.snapshot or self.loaded_generation < self.update_generation
        fallback_stale = (
            time.monotonic() - self.received_at >= max_age and (
                not marker_connected
                or Path(settings.data_dir).resolve() != self._repo.store.data_dir.resolve()
            )
        )
        if stale or fallback_stale:
            return self.refresh(force=False, max_age=max_age)
        return self.status()

    def update(self, snapshot):
        with measure_runtime_lock(self.refresh_lock, "public.refresh"):
            if self.snapshot and snapshot.get("captured_at", 0) < self.snapshot.get("captured_at", 0):
                return
            return self._update(snapshot, generation=self.update_generation)

    def _fetch_snapshot(self):
        try:
            snapshot = self.market.snapshot()
        except Exception:
            raise PublicReleaseError("snapshot_unavailable") from None
        self.snapshot_fetches += 1
        return snapshot

    def sync_public_release(self, *, stopped=None, clock=time.monotonic, max_age=30):
        target = current_release(settings.data_dir)
        if target == self.bound_release():
            return "unchanged"
        with measure_runtime_stage("public.refresh_wait"):
            acquired = self.refresh_lock.acquire(blocking=False)
        if not acquired:
            return "busy"
        try:
            with measure_runtime_stage("public.refresh"):
                target = current_release(settings.data_dir)
                if target == self.bound_release():
                    return "unchanged"
                started = clock()
                generation = self.update_generation
                snapshot = self._fetch_snapshot()
                self._update(
                    snapshot, generation=generation, expected=target,
                    stopped=stopped, clock=clock, deadline=started + max_age,
                )
                return "updated"
        finally:
            self.refresh_lock.release()

    def _prepare(self, snapshot, expected):
        frames = {asset: decode_frame(encoded) for asset, encoded in snapshot["frames"].items()}
        if (
            "stock" not in frames or set(frames) - {"stock", "index"}
            or (snapshot.get("public_release") and "index" not in frames)
        ):
            raise PublicReleaseError("prepare_failed")
        indices = decode_frame(snapshot["indices"])
        self.frame_decodes += len(frames) + 1
        repo = self._repo
        release = snapshot.get("public_release")
        new_repository = False
        if release:
            if not isinstance(release, dict) or not isinstance(release.get("id"), str) or not RELEASE_ID.fullmatch(release["id"]):
                raise PublicReleaseError()
            target = current_release(settings.data_dir)
            if target.name != release["id"] or (expected is not None and target != expected):
                raise PublicReleaseError("snapshot_release_mismatch")
            if repo.store.data_dir != target:
                repo = open_public_repository(target)
                new_repository = True
            else:
                attach_public_lease(repo.store).validate()
        elif expected is not None:
            raise PublicReleaseError("snapshot_release_mismatch")
        candidate = PreparedPublicBinding(repo, snapshot, frames, indices, None, None, new_repository)
        try:
            snapshot["status"].get("symbol_count", 0)
            candidate.depth = PublicDepthBridge(repo)
            candidate.depth.update(snapshot.get("depth"))
            if self.state is not None:
                from app.services.sector_monitor import SectorMonitorService

                candidate.sector = getattr(self.state, "sector_monitor_service", None)
                if getattr(candidate.sector, "_repo", None) is not repo:
                    candidate.sector = SectorMonitorService(repo)
            if new_repository:
                for asset, frame in frames.items():
                    repo.install_public_snapshot(asset, frame)
            return candidate
        except BaseException:
            candidate.close()
            raise

    def _update(self, snapshot, *, generation, expected=None, stopped=None, clock=time.monotonic, deadline=None):
        if self._closed.is_set():
            raise PublicReleaseError("stopped")
        with measure_runtime_stage("public.binding_prepare"):
            candidate = self._prepare(snapshot, expected)
        try:
            release = snapshot.get("public_release")
            if release and current_release(settings.data_dir).name != release["id"]:
                raise PublicReleaseError("snapshot_release_mismatch")
            if deadline is not None and clock() > deadline:
                raise PublicReleaseError("sync_stalled")
            with measure_runtime_stage("public.binding_commit"), self.update_lock:
                if self._closed.is_set() or (stopped is not None and stopped.is_set()):
                    raise PublicReleaseError("stopped")
                if release and current_release(settings.data_dir).name != release["id"]:
                    raise PublicReleaseError("snapshot_release_mismatch")
                previous = self.snapshot
                repo = candidate.repo
                if not candidate.new_repository:
                    for asset, frame in candidate.frames.items():
                        repo.install_public_snapshot(asset, frame)
                self._repo = repo
                # Keep the externally held depth service stable for same-release
                # updates. Parsing already succeeded outside the update lock.
                if candidate.new_repository:
                    self.depth = candidate.depth
                elif snapshot.get("depth"):
                    with self.depth._lock:
                        for name in ("_sealed_cache", "_sealed_date", "_sealed_ready", "_sealed_fetched_ts"):
                            setattr(self.depth, name, getattr(candidate.depth, name))
                if self.state is not None:
                    self.state.repo, self.state.datastore = repo, repo.store
                    self.state.depth_service = self.depth
                    self.state.sector_monitor_service = candidate.sector
                    self.state.indicators_ready = not candidate.frames["stock"].is_empty()
                with self._lock:
                    self._index_quotes_cache = candidate.indices
                self._symbol_count = snapshot["status"].get("symbol_count", 0)
                self.snapshot, self.received_at = snapshot, time.monotonic()
                self.loaded_generation = generation
                candidate.committed = True
        finally:
            candidate.close()
        if snapshot.get("public_release"):
            self.binding_sync._observe(current_release(settings.data_dir))
        # Delivery failure cannot revoke ownership of an installed repository.
        try:
            self._notify_update(previous, snapshot)
        except Exception:
            logger.warning("public binding notification failed kind=prepare_failed")

    def _notify_update(self, previous, snapshot):
        if (previous is None or previous["sequence"] != snapshot["sequence"]
                or previous.get("public_release") != snapshot.get("public_release")):
            self._broadcast_quote_updated()
        self.notify_depth_updated()

    def mark_event(self, event):
        with self.update_lock:
            self.update_generation += 1
        if event == "quotes_updated":
            self._broadcast_quote_updated()
        elif event == "depth_updated":
            self.notify_depth_updated()

    def bridge_status(self):
        marker_status = self.markers.status() if self.markers is not None else {"connected": False}
        return {
            **marker_status,
            "update_generation": self.update_generation,
            "loaded_generation": self.loaded_generation,
            "snapshot_fetches": self.snapshot_fetches,
            "frame_decodes": self.frame_decodes,
        }


class PublicDepthBridge(DepthService):
    """Preserve live depth and historical-file reads without any provider call."""
    def __init__(self, repo):
        super().__init__()
        self.set_repo(repo)

    def update(self, snapshot):
        if not snapshot:
            return
        from datetime import date

        with self._lock:
            self._sealed_cache = snapshot["entries"]
            self._sealed_date = date.fromisoformat(snapshot["day"]) if snapshot.get("day") else None
            self._sealed_ready = bool(snapshot["ready"])
            age = snapshot.get("age")
            self._sealed_fetched_ts = time.perf_counter() - age if age is not None else 0

    def boot_check(self):
        return

    def start_polling(self):
        return
