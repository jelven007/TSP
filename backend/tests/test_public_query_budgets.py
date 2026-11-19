"""A stalled owner must not consume every caller's time or spawn duplicate scans."""
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Event

import pytest

from app.services import public_query_cache as module


def test_wait_budget_does_not_replace_owner_and_other_keys_still_load():
    cache = module.PublicQueryCache(wait_timeout=0.02, max_inflight=2)
    entered, finish = Event(), Event()

    def load():
        entered.set()
        assert finish.wait(3)
        return "original"

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(cache.get, "data", "slow", 30, load)
        assert entered.wait(1)
        try:
            for _ in range(2):
                with pytest.raises(module.CacheWaitTimeoutError):
                    cache.get("data", "slow", 30, lambda: pytest.fail("duplicate scan"))
            assert cache.get("data", "other", 30, lambda: "other") == "other"
            assert cache.snapshot()["inflight"] == 1
        finally:
            finish.set()
        assert owner.result(timeout=1) == "original"
    assert cache.get("data", "slow", 30, lambda: "wrong") == "original"
    stats = cache.snapshot()
    assert stats["timeouts"] == 2
    assert stats["loads"] == 2
    assert stats["hits"] == 1
    assert stats["inflight"] == 0


def test_capacity_survives_invalidation_and_failure_releases_slot():
    cache = module.PublicQueryCache(max_inflight=1)
    entered, finish = Event(), Event()

    def fail():
        entered.set()
        assert finish.wait(3)
        raise ValueError("failed scan")

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(cache.get, "data", "root", 30, fail)
        assert entered.wait(1)
        try:
            cache.invalidate()
            with pytest.raises(module.CacheBusyError):
                cache.get("data", "root", 30, lambda: pytest.fail("exceeded budget"))
        finally:
            finish.set()
        with pytest.raises(ValueError, match="failed scan"):
            owner.result(timeout=1)
    assert cache.get("data", "root", 30, lambda: "new") == "new"
    assert cache.snapshot()["failures"] == 1
    assert cache.snapshot()["rejected"] == 1


def test_cancelled_waiter_leaves_owner_intact():
    cache = module.PublicQueryCache()
    entered, finish, cancelled = Event(), Event(), Event()

    def load():
        entered.set()
        assert finish.wait(3)
        return 42

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.get, "data", "key", 30, load)
        assert entered.wait(1)
        waiter = pool.submit(
            cache.get, "data", "key", 30, lambda: pytest.fail("duplicate"),
            cancel_event=cancelled,
        )
        try:
            cancelled.set()
            with pytest.raises(CancelledError):
                waiter.result(timeout=1)
        finally:
            finish.set()
        assert owner.result(timeout=1) == 42
    assert cache.snapshot()["cancelled"] == 1


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"max_inflight": 0}, {"wait_timeout": -1},
    {"wait_timeout": float("inf")}, {"wait_timeout": float("nan")},
])
def test_invalid_budgets_rejected(kwargs):
    with pytest.raises(ValueError):
        module.PublicQueryCache(**kwargs)


def test_main_maps_cache_budget_failure_to_retryable_http_response():
    from fastapi.testclient import TestClient

    from app.main import app

    handler = app.exception_handlers.get(module.CacheUnavailableError)
    assert handler is not None
    from fastapi import FastAPI
    isolated = FastAPI(exception_handlers={module.CacheUnavailableError: handler})

    @isolated.get("/read")
    def read():
        raise module.CacheWaitTimeoutError("private cache key must not leak")

    response = TestClient(isolated).get("/read")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["code"] == "public_query_unavailable"
    assert "private cache key" not in response.text
