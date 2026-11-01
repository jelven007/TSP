"""Publication-only polling per API process, independent of request traffic."""
from __future__ import annotations

import logging
import threading
import time

from app.config import settings
from app.multitenancy.public_reader import PublicReleaseError, current_release
from app.services.observability import measure_runtime_stage

logger = logging.getLogger(__name__)
ERROR_KINDS = frozenset({
    "current_unavailable", "invalid_release", "reader_lock_busy", "snapshot_unavailable",
    "snapshot_release_mismatch", "prepare_failed", "sync_stalled", "stopped",
})


class PublicBindingSynchronizer:
    def __init__(self, bridge, *, interval=5, max_age=30, clock=time.monotonic):
        self.bridge, self.interval, self.max_age, self.clock = bridge, interval, max_age, clock
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self.thread = None
        self._checked = None
        self._mismatch = None
        self._binding_current = False
        self._error = None
        self.lock_busy_count = 0

    def start(self):
        with self._lock:
            if self.stopped.is_set() or self.thread is not None:
                return
            self.thread = threading.Thread(target=self._loop, name="public-binding-sync", daemon=True)
            self.thread.start()

    def _loop(self):
        while not self.stopped.is_set():
            self.check_once()
            if self.stopped.wait(self.interval):
                break

    def _observe(self, target):
        bound = self.bridge.bound_release()
        now = self.clock()
        with self._lock:
            self._checked = now
            self._binding_current = target == bound
            if self._binding_current:
                self._mismatch = None
                self._error = None
            elif self._mismatch is None:
                self._mismatch = now

    def check_once(self):
        if self.stopped.is_set():
            return
        try:
            with measure_runtime_stage("public.binding_check"):
                target = current_release(settings.data_dir)
                self._observe(target)
                if target != self.bridge.bound_release():
                    result = self.bridge.sync_public_release(
                        stopped=self.stopped, clock=self.clock, max_age=self.max_age,
                    )
                    if result == "busy":
                        with self._lock:
                            self.lock_busy_count = min(self.lock_busy_count + 1, 2**31 - 1)
                    self._observe(current_release(settings.data_dir))
        except Exception as exc:
            kind = exc.kind if isinstance(exc, PublicReleaseError) else "prepare_failed"
            kind = kind if kind in ERROR_KINDS else "prepare_failed"
            with self._lock:
                self._error = kind
            logger.warning("public binding synchronization failed kind=%s", kind)

    def snapshot(self):
        now = self.clock()
        with self._lock:
            age = None if self._checked is None else max(0, now - self._checked)
            mismatch = None if self._mismatch is None else max(0, now - self._mismatch)
            error = self._error
            if self.stopped.is_set():
                error = "stopped"
            elif age is None or age > self.max_age or (mismatch is not None and mismatch > self.max_age):
                error = "sync_stalled"
            return {
                "running": bool(self.thread and self.thread.is_alive() and not self.stopped.is_set()),
                "last_checked_age_seconds": age, "binding_current": self._binding_current,
                "mismatch_age_seconds": mismatch, "last_error_kind": error,
                "lock_busy_count": self.lock_busy_count,
            }

    def close(self, *, timeout=20):
        self.stopped.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if timeout and thread.is_alive():
                logger.warning("public binding synchronization shutdown kind=sync_stalled")
