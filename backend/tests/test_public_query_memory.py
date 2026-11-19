"""Resident payload bounds and cooperative loading without orphaned owners."""
import sys
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Event

import pytest

from app.services import public_query_cache as module


def test_byte_budget_evicts_lru_even_below_entry_limit():
    cache = module.PublicQueryCache(limit=128, max_bytes=1500)
    for key in ("a", "b", "c"):
        cache.get("data", key, 30, lambda: bytes(500))
    assert cache.snapshot()["entries"] < 3
    assert 0 < cache.snapshot()["resident_bytes"] <= 1500
    assert cache.get("data", "a", 30, lambda: b"reloaded") == b"reloaded"


def test_nested_payload_and_identity_count_towards_budget():
    cache = module.PublicQueryCache(max_bytes=2000)
    payload = {"rows": [{"name": "x" * 4000}]}
    assert cache.get("data", "small-key", 30, lambda: payload) is payload
    assert cache.snapshot()["entries"] == 0
    assert cache.get("data", "k" * 4000, 30, lambda: 1) == 1
    assert cache.snapshot()["entries"] == 0
    assert cache.snapshot()["oversized"] == 2


def test_size_accounts_shared_references_once_and_handles_cycles():
    payload = []
    payload.append(payload)
    assert module.estimate_cache_bytes(payload, 10_000) == sys.getsizeof(payload)
    value = bytes(100)
    shared = [value, value]
    assert module.estimate_cache_bytes(shared, 10_000) == (
        sys.getsizeof(shared) + sys.getsizeof(value)
    )


def test_invalidation_and_expired_replacement_release_bytes():
    cache = module.PublicQueryCache(max_bytes=10_000)
    cache.get("data:daily", "root", 30, lambda: bytes(1000))
    cache.get("storage", "root", 30, lambda: bytes(1000))
    before = cache.snapshot()["resident_bytes"]
    cache.invalidate({"data"})
    assert 0 < cache.snapshot()["resident_bytes"] < before
    cache.get("storage", "root", 0, lambda: None)
    assert cache.snapshot()["resident_bytes"] < before / 2
    cache.invalidate()
    assert cache.snapshot()["resident_bytes"] == 0


def test_unsupported_object_is_returned_without_unbounded_caching():
    cache = module.PublicQueryCache()
    payload = object()
    assert cache.get("data", "key", 30, lambda: payload) is payload
    assert cache.snapshot()["entries"] == 0
    assert cache.snapshot()["uncacheable"] == 1


def test_owner_deadline_rejects_late_value_and_releases_capacity(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    cache = module.PublicQueryCache(load_timeout=1, max_inflight=1)

    def late_load():
        clock[0] += 2
        return "too late"

    with pytest.raises(module.CacheWaitTimeoutError):
        cache.get("data", "key", 30, late_load)
    assert cache.snapshot()["inflight"] == cache.snapshot()["entries"] == 0
    assert cache.get("data", "key", 30, lambda: "fresh") == "fresh"
    assert cache.snapshot()["load_timeouts"] == 1


def test_checkpoint_cancels_owner_before_next_scan_and_context_is_reset():
    event = Event()
    cache = module.PublicQueryCache()

    def scan():
        event.set()
        module.public_query_checkpoint()
        pytest.fail("continued scan after cancellation")

    with pytest.raises(CancelledError):
        cache.get("storage", "root", 30, scan, cancel_event=event)
    assert cache.snapshot()["inflight"] == cache.snapshot()["entries"] == 0
    module.public_query_checkpoint()  # no stale cancelled context outside the load


def test_nested_load_cannot_extend_parent_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    outer = module.PublicQueryCache(load_timeout=1)
    inner = module.PublicQueryCache(load_timeout=60)

    def nested():
        clock[0] += 2
        module.public_query_checkpoint()
        pytest.fail("nested loader extended its caller's deadline")

    with pytest.raises(module.CacheWaitTimeoutError):
        outer.get("data", "outer", 30, lambda: inner.get("data", "inner", 30, nested))
    assert inner.snapshot()["inflight"] == outer.snapshot()["inflight"] == 0


def test_nested_waiter_obeys_parent_deadline_without_replacing_inner_owner():
    outer = module.PublicQueryCache(load_timeout=.03)
    inner = module.PublicQueryCache(wait_timeout=3)
    entered, finish = Event(), Event()

    def load():
        entered.set()
        assert finish.wait(2)
        return "owned"

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(inner.get, "data", "key", 30, load)
        assert entered.wait(1)
        nested = pool.submit(
            outer.get, "data", "outer", 30,
            lambda: inner.get("data", "key", 30, lambda: pytest.fail("duplicate owner")),
        )
        try:
            with pytest.raises(module.CacheWaitTimeoutError):
                nested.result(timeout=.5)
            assert inner.snapshot()["inflight"] == 1
        finally:
            finish.set()
        assert owner.result(timeout=1) == "owned"


def test_storage_scan_stops_before_stat_after_cancellation(tmp_path, monkeypatch):
    from app.api import data

    (tmp_path / "file").write_bytes(b"fixture")
    event = Event()
    scandir = data.os.scandir

    class GuardedEntry:
        def is_dir(self, **_):
            pytest.fail("filesystem scan continued after cancellation")

    def cancelled_scan(path):
        with scandir(path) as entries:
            for _ in entries:
                event.set()
                yield GuardedEntry()

    monkeypatch.setattr(data.os, "scandir", cancelled_scan)
    cache = module.PublicQueryCache()
    with pytest.raises(CancelledError):
        cache.get("storage", "root", 30, lambda: data._scan_dir_stats(tmp_path),
                  cancel_event=event)
    assert cache.snapshot()["inflight"] == 0


@pytest.mark.parametrize("kwargs", [
    {"max_bytes": 0}, {"max_bytes": -1}, {"load_timeout": -1},
    {"load_timeout": float("inf")}, {"load_timeout": float("nan")},
])
def test_invalid_memory_and_load_budgets(kwargs):
    with pytest.raises(ValueError):
        module.PublicQueryCache(**kwargs)
