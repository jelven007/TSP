"""Bounded caches for public data only, partitioned by immutable data root.

Private results must use tenant/revision/authorization scoped storage instead.
Invalidation generations prevent a slow read from reinstalling a stale value.
Concurrent readers share a fetch only within the same identity and generation.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import CancelledError
from contextvars import ContextVar
from datetime import date, datetime
from itertools import chain
from typing import TypeVar, cast

T = TypeVar("T")


class CacheUnavailableError(RuntimeError):
    """A public read could not acquire its bounded cache budget."""


class CacheWaitTimeoutError(CacheUnavailableError, TimeoutError):
    pass


class CacheBusyError(CacheUnavailableError):
    pass


_load_context: ContextVar[tuple[float, tuple[threading.Event, ...]] | None] = ContextVar(
    "public_query_load_context", default=None,
)


def public_query_checkpoint() -> None:
    """Stop between I/O calls; an already running synchronous I/O is not killed."""
    context = _load_context.get()
    if context is not None:
        deadline, events = context
        if any(event.is_set() for event in events):
            raise CancelledError()
        if time.monotonic() >= deadline:
            raise CacheWaitTimeoutError("public query load budget exhausted")


def estimate_cache_bytes(value: object, ceiling: int) -> int | None:
    """Bounded, cycle-safe retained-size estimate for public response values.

    Unknown/native objects are not cached. Shared references count once per entry;
    shared values across entries deliberately count again. This is not RSS, and
    callers must not mutate a value after publishing it to the cache.
    """
    seen: set[int] = set()
    pending = [iter((value,))]
    size = 0
    while pending:
        public_query_checkpoint()
        try:
            item = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if id(item) in seen:
            continue
        seen.add(id(item))
        kind = type(item)
        if kind not in (dict, list, tuple, set, frozenset, str, bytes, int, float,
                        bool, type(None), date, datetime):
            return None
        size += sys.getsizeof(item)
        if size > ceiling:
            return size
        if isinstance(item, dict):
            pending.append(chain.from_iterable(item.items()))
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.append(iter(item))
    return size


class PublicQueryCache:
    def __init__(
        self, limit: int = 128, *, wait_timeout: float = 30.0, max_inflight: int = 16,
        max_bytes: int = 32 * 1024 * 1024, load_timeout: float = 30.0,
    ):
        if limit < 1 or max_inflight < 1 or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("cache limits must be positive")
        if not math.isfinite(wait_timeout) or wait_timeout < 0:
            raise ValueError("wait_timeout must be finite and nonnegative")
        if not math.isfinite(load_timeout) or load_timeout < 0:
            raise ValueError("load_timeout must be finite and nonnegative")
        self._limit = limit
        self._wait_timeout = wait_timeout
        self._max_inflight = max_inflight
        self._max_bytes = max_bytes
        self._resident_bytes = 0
        self._load_timeout = load_timeout
        self._stats = dict.fromkeys(
            ("hits", "loads", "waits", "timeouts", "cancelled", "rejected", "failures",
             "load_timeouts", "oversized", "uncacheable", "evictions"), 0,
        )
        self._load_ms = 0.0
        self._wait_ms = 0.0
        self._lock = threading.Lock()
        self._generation = 0
        self._namespace_generations: dict[str, int] = {}
        self._entries: OrderedDict[tuple[str, Hashable], tuple[float, object, int]] = OrderedDict()
        self._inflight: dict[
            tuple[tuple[str, Hashable], tuple[int, int, int]], threading.Event
        ] = {}

    def _generation_for(self, namespace: str) -> tuple[int, int, int]:
        return (
            self._generation,
            self._namespace_generations.get(namespace, 0),
            self._namespace_generations.get(namespace.split(":")[0], 0),
        )

    def get(
        self, namespace: str, key: Hashable, ttl: float, fetch: Callable[[], T],
        *, wait_timeout: float | None = None, cancel_event: threading.Event | None = None,
    ) -> T:
        """Share a synchronous load; waiter timeout never replaces a running owner.

        Running fetches retain their slots until they exit, even after invalidation.
        Loaders can call public_query_checkpoint between I/O operations. Late or
        cancelled loads are not published. No background threads are created.
        """
        budget = self._wait_timeout if wait_timeout is None else wait_timeout
        if not math.isfinite(budget) or budget < 0:
            raise ValueError("wait_timeout must be finite and nonnegative")
        deadline = time.monotonic() + budget
        parent = _load_context.get()
        if parent is not None:
            deadline = min(deadline, parent[0])
        waited = False
        identity = (namespace, key)
        while True:
            public_query_checkpoint()
            with self._lock:
                if cancel_event is not None and cancel_event.is_set():
                    self._stats["cancelled"] += 1
                    raise CancelledError()
                if waited and time.monotonic() >= deadline:
                    self._stats["timeouts"] += 1
                    raise CacheWaitTimeoutError("public query wait budget exhausted")
                entry = self._entries.get(identity)
                if entry is not None and time.monotonic() - entry[0] < ttl:
                    self._stats["hits"] += 1
                    self._entries.move_to_end(identity)
                    return cast(T, entry[1])
                if entry is not None:
                    self._resident_bytes -= self._entries.pop(identity)[2]
                generation = self._generation_for(namespace)
                flight_key = (identity, generation)
                pending = self._inflight.get(flight_key)
                if pending is None:
                    if len(self._inflight) >= self._max_inflight:
                        self._stats["rejected"] += 1
                        raise CacheBusyError("public query load capacity exhausted")
                    pending = threading.Event()
                    self._inflight[flight_key] = pending
                    self._stats["loads"] += 1
                    break
                if not waited:
                    self._stats["waits"] += 1
                waited = True
            started_wait = time.monotonic()
            remaining = max(0.0, deadline - started_wait)
            cancellable = cancel_event is not None or (parent is not None and bool(parent[1]))
            pending.wait(min(remaining, 0.05) if cancellable else remaining)
            with self._lock:
                self._wait_ms += (time.monotonic() - started_wait) * 1000

        started_load = time.monotonic()
        load_deadline = started_load + self._load_timeout
        events: tuple[threading.Event, ...] = (cancel_event,) if cancel_event is not None else ()
        if parent is not None:
            load_deadline = min(load_deadline, parent[0])
            events += parent[1]
        token = _load_context.set((load_deadline, events))
        try:
            public_query_checkpoint()
            fresh = fetch()
            public_query_checkpoint()
            # Include identity and a conservative per-entry bookkeeping allowance.
            size = estimate_cache_bytes((identity, fresh), self._max_bytes)
            size = size + 256 if size is not None else None
            public_query_checkpoint()
            with self._lock:
                if size is None:
                    self._stats["uncacheable"] += 1
                elif size > self._max_bytes:
                    self._stats["oversized"] += 1
                elif generation == self._generation_for(namespace):
                    self._entries[identity] = (time.monotonic(), fresh, size)
                    self._resident_bytes += size
                    self._entries.move_to_end(identity)
                    while len(self._entries) > self._limit or self._resident_bytes > self._max_bytes:
                        _, removed = self._entries.popitem(last=False)
                        self._resident_bytes -= removed[2]
                        self._stats["evictions"] += 1
            return fresh
        except BaseException as exc:
            with self._lock:
                self._stats["failures"] += 1
                if isinstance(exc, CacheWaitTimeoutError):
                    self._stats["load_timeouts"] += 1
                elif isinstance(exc, CancelledError):
                    self._stats["cancelled"] += 1
            raise
        finally:
            _load_context.reset(token)
            with self._lock:
                self._load_ms += (time.monotonic() - started_load) * 1000
                del self._inflight[flight_key]
            pending.set()

    def snapshot(self) -> dict[str, int | float]:
        """Aggregate counters only: no cache keys, paths or private identities."""
        with self._lock:
            return {
                **self._stats, "entries": len(self._entries), "inflight": len(self._inflight),
                "entry_limit": self._limit, "inflight_limit": self._max_inflight,
                "wait_timeout_seconds": self._wait_timeout,
                "load_timeout_seconds": self._load_timeout,
                "resident_bytes": self._resident_bytes, "byte_limit": self._max_bytes,
                "load_ms": round(self._load_ms, 3), "wait_ms": round(self._wait_ms, 3),
            }

    def invalidate(self, namespaces: set[str] | None = None) -> None:
        with self._lock:
            if namespaces is None:
                self._generation += 1
                self._namespace_generations.clear()
                self._entries.clear()
                self._resident_bytes = 0
            else:
                for namespace in namespaces:
                    self._namespace_generations[namespace] = (
                        self._namespace_generations.get(namespace, 0) + 1
                    )
                for key in list(self._entries):
                    if key[0] in namespaces or key[0].split(":")[0] in namespaces:
                        self._resident_bytes -= self._entries.pop(key)[2]


public_query_cache = PublicQueryCache()


def invalidate_data_cache(table: str | None = None) -> None:
    if table is None:
        public_query_cache.invalidate({"storage", "jobs", "data"})
    else:
        namespaces = {f"data:{table}", "data:quality"}
        if table == "instruments":
            namespaces.add("jobs")
        public_query_cache.invalidate(namespaces)


def invalidate_storage_cache() -> None:
    invalidate_data_cache()


def invalidate_overview_cache() -> None:
    public_query_cache.invalidate({"overview"})


def invalidate_regime_cache() -> None:
    public_query_cache.invalidate({"regime"})
