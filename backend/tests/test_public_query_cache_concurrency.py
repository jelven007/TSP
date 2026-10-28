"""Background invalidation and concurrent polling preserve table isolation."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest

from app.services.public_query_cache import PublicQueryCache


@pytest.mark.parametrize("namespaces", [{"data:minute", "data:quality"}, {"overview"}])
def test_unrelated_stage_invalidation_keeps_inflight_table_result(namespaces):
    cache = PublicQueryCache()
    entered, finish = Event(), Event()

    def read_daily():
        entered.set()
        assert finish.wait(3)
        return {"rows": 42}

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cache.get, "data:daily", "root", 30, read_daily)
        assert entered.wait(1)
        cache.invalidate(namespaces)
        finish.set()
        assert pending.result(timeout=1) == {"rows": 42}

    def repeated_scan():
        pytest.fail("minute publication discarded an unrelated completed daily scan")

    assert cache.get("data:daily", "root", 30, repeated_scan) == {"rows": 42}


def test_concurrent_status_misses_share_one_scan():
    cache = PublicQueryCache()
    entered, finish, waiter_started = Event(), Event(), Event()

    def read_daily():
        entered.set()
        assert finish.wait(3)
        return {"rows": 42}

    def duplicate_scan():
        pytest.fail("concurrent status readers performed the same expensive scan twice")

    def waiting_read():
        waiter_started.set()
        return cache.get("data:daily", "root", 30, duplicate_scan)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.get, "data:daily", "root", 30, read_daily)
        assert entered.wait(1)
        waiter = pool.submit(waiting_read)
        assert waiter_started.wait(1)
        try:
            with pytest.raises(TimeoutError):
                waiter.result(timeout=0.2)
        finally:
            finish.set()
        assert owner.result(timeout=1) == waiter.result(timeout=1) == {"rows": 42}


def test_other_table_read_does_not_wait_for_slow_scan():
    cache = PublicQueryCache()
    entered, finish = Event(), Event()

    def read_daily():
        entered.set()
        assert finish.wait(3)
        return "daily"

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.get, "data:daily", "root", 30, read_daily)
        assert entered.wait(1)
        try:
            other = pool.submit(cache.get, "data:instruments", "root", 30, lambda: "catalog")
            assert other.result(timeout=1) == "catalog"
        finally:
            finish.set()
        assert owner.result(timeout=1) == "daily"


def test_other_data_root_does_not_wait_for_slow_scan():
    cache = PublicQueryCache()
    entered, finish = Event(), Event()

    def read_first_root():
        entered.set()
        assert finish.wait(3)
        return "first"

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.get, "data:daily", "/release/one", 30, read_first_root)
        assert entered.wait(1)
        try:
            other = pool.submit(cache.get, "data:daily", "/release/two", 30, lambda: "second")
            assert other.result(timeout=1) == "second"
        finally:
            finish.set()
        assert owner.result(timeout=1) == "first"


@pytest.mark.parametrize("namespaces", [{"data:daily"}, {"data"}, None])
def test_invalidated_table_reads_new_generation_without_waiting(namespaces):
    cache = PublicQueryCache()
    entered, finish = Event(), Event()

    def old_read():
        entered.set()
        assert finish.wait(3)
        return "old"

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.get, "data:daily", "root", 30, old_read)
        assert entered.wait(1)
        try:
            cache.invalidate(namespaces)
            refreshed = pool.submit(cache.get, "data:daily", "root", 30, lambda: "new")
            assert refreshed.result(timeout=1) == "new"
        finally:
            finish.set()
        assert owner.result(timeout=1) == "old"

    assert cache.get("data:daily", "root", 30, lambda: "unexpected") == "new"


def test_failed_scan_releases_waiters_to_retry():
    cache = PublicQueryCache()
    entered, finish, waiter_started = Event(), Event(), Event()

    def failed_read():
        entered.set()
        assert finish.wait(3)
        raise ValueError("scan failed")

    def waiting_read():
        waiter_started.set()
        return cache.get("data:daily", "root", 30, lambda: "recovered")

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.get, "data:daily", "root", 30, failed_read)
        assert entered.wait(1)
        waiter = pool.submit(waiting_read)
        assert waiter_started.wait(1)
        try:
            with pytest.raises(TimeoutError):
                waiter.result(timeout=0.2)
        finally:
            finish.set()
        with pytest.raises(ValueError, match="scan failed"):
            owner.result(timeout=1)
        assert waiter.result(timeout=1) == "recovered"

    assert cache.get("data:daily", "root", 30, lambda: "unexpected") == "recovered"
