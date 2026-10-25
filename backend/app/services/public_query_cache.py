"""Bounded caches for public data only, partitioned by immutable data root.

Private results must use tenant/revision/authorization scoped storage instead.
Invalidation generations prevent a slow read from reinstalling a stale value.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import TypeVar, cast

T = TypeVar("T")


class PublicQueryCache:
    def __init__(self, limit: int = 128):
        self._limit = limit
        self._lock = threading.Lock()
        self._generation = 0
        self._entries: OrderedDict[tuple[str, Hashable], tuple[float, object]] = OrderedDict()

    def get(self, namespace: str, key: Hashable, ttl: float, fetch: Callable[[], T]) -> T:
        identity = (namespace, key)
        with self._lock:
            entry = self._entries.get(identity)
            if entry is not None and time.monotonic() - entry[0] < ttl:
                self._entries.move_to_end(identity)
                return cast(T, entry[1])
            generation = self._generation
        fresh = fetch()
        with self._lock:
            if generation == self._generation:
                self._entries[identity] = (time.monotonic(), fresh)
                self._entries.move_to_end(identity)
                while len(self._entries) > self._limit:
                    self._entries.popitem(last=False)
        return fresh

    def invalidate(self, namespaces: set[str] | None = None) -> None:
        with self._lock:
            self._generation += 1
            if namespaces is None:
                self._entries.clear()
            else:
                for key in list(self._entries):
                    if key[0] in namespaces or key[0].split(":")[0] in namespaces:
                        del self._entries[key]


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
