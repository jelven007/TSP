"""A single public market reader per full API process; original read/SSE contract."""
from __future__ import annotations

import json
import threading
import time
import weakref
from pathlib import Path

from app.config import settings
from app.multitenancy.market import decode_frame
from app.services.depth_service import DepthService
from app.services.quote_service import QuoteService


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

    def start(self):
        if self.markers is not None:
            self.markers.start()

    def attach_markers(self, markers):
        self.markers = markers

    def close(self):
        if self.markers is not None:
            self.markers.close()
        self.market.close()

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
        with self.refresh_lock:
            marker_connected = bool(self.markers and self.markers.status()["connected"])
            stale = not self.snapshot or self.loaded_generation < self.update_generation
            fallback_stale = not marker_connected and time.monotonic() - self.received_at >= max_age
            if not force and not stale and not fallback_stale:
                return self.status()
            generation = self.update_generation
            snapshot = self.market.snapshot()
            self.snapshot_fetches += 1
            with self.update_lock:
                self._update(snapshot)
                self.loaded_generation = generation
            return self.status()

    def ensure_fresh(self, max_age=5):
        marker_connected = bool(self.markers and self.markers.status()["connected"])
        stale = not self.snapshot or self.loaded_generation < self.update_generation
        fallback_stale = not marker_connected and time.monotonic() - self.received_at >= max_age
        if stale or fallback_stale:
            return self.refresh(force=False, max_age=max_age)
        return self.status()

    def update(self, snapshot):
        with self.update_lock:
            if self.snapshot and snapshot.get("captured_at", 0) < self.snapshot.get("captured_at", 0):
                return
            result = self._update(snapshot)
            self.loaded_generation = self.update_generation
            return result

    def _update(self, snapshot):
        previous = self.snapshot
        frames = {asset: decode_frame(encoded) for asset, encoded in snapshot["frames"].items()}
        indices = decode_frame(snapshot["indices"])
        self.frame_decodes += len(frames) + 1
        repo = self._repo
        release = snapshot.get("public_release")
        if release and (not previous or previous.get("public_release", {}).get("id") != release["id"]):
            from app.tickflow.repository import DataStore, KlineRepository

            root = Path(settings.data_dir).resolve()
            # DATA_DIR is /public/current; only the sibling releases are accepted.
            target = root.parent / release["id"]
            if len(release["id"]) != 32 or any(char not in "0123456789abcdef" for char in release["id"]):
                raise ValueError("invalid public release")
            target = target.resolve(strict=True)
            if target.parent != root.parent:
                raise ValueError("invalid public release path")
            marker = json.loads((target / ".public_release.json").read_text())
            if marker["id"] != release["id"]:
                raise ValueError("public release marker mismatch")
            store = DataStore(target, read_only=True)
            weakref.finalize(store, store.db.close)
            repo = KlineRepository(store)
            repo._refresh_instruments()
            repo._refresh_index_instruments()
            repo._refresh_etf_instruments()
        for asset, frame in frames.items():
            repo.install_public_snapshot(asset, frame)
        self.set_repo(repo)
        self.depth.set_repo(repo)
        self.depth.update(snapshot.get("depth"))
        if self.state is not None:
            from app.services.sector_monitor import SectorMonitorService

            self.state.repo, self.state.datastore = repo, repo.store
            self.state.indicators_ready = not frames["stock"].is_empty()
            if repo is not getattr(getattr(self.state, "sector_monitor_service", None), "_repo", None):
                self.state.sector_monitor_service = SectorMonitorService(repo)
        with self._lock:
            self._index_quotes_cache = indices
        self._symbol_count = snapshot["status"].get("symbol_count", 0)
        self.snapshot, self.received_at = snapshot, time.monotonic()
        if (previous is None or previous["sequence"] != snapshot["sequence"]
                or previous.get("public_release") != snapshot.get("public_release")):
            self._broadcast_quote_updated()
        # Depth can change independently of the stock quote cycle.
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
