import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import polars as pl

from app.backtest.engine import PanelCache

DAY = date(2026, 1, 1)


def get(cache, key, compute):
    return cache.get_or_compute([key], DAY, DAY, None, compute)


def test_byte_limit_evicts_oldest_panels_without_changing_results():
    frame = pl.DataFrame({"close": list(range(1000))})
    cache = PanelCache(max_size=4, max_bytes=frame.estimated_size() * 2)
    calls = []

    def compute(symbols, *_):
        calls.append(symbols[0])
        return frame.clone()

    for key in ("a", "b", "a", "c", "a", "b"):
        assert get(cache, key, compute).equals(frame)
    assert calls == ["a", "b", "c", "b"]
    assert cache.stats()["retained_bytes"] <= frame.estimated_size() * 2
    cache.invalidate()
    assert cache.stats()["retained_bytes"] == 0


def test_oversized_panel_is_returned_but_not_retained():
    frame = pl.DataFrame({"close": list(range(1000))})
    cache = PanelCache(max_bytes=frame.estimated_size() - 1)
    calls = []

    def compute(*_):
        calls.append(True)
        return frame

    assert get(cache, "a", compute) is frame
    assert get(cache, "a", compute) is frame
    assert len(calls) == 2
    assert cache.stats()["retained_bytes"] == 0


def test_expired_unrelated_panels_are_released_on_next_request(monkeypatch):
    from app.backtest import engine

    clock = [0.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    frame = pl.DataFrame({"close": list(range(100))})
    cache = PanelCache(max_size=10, ttl_seconds=10, max_bytes=frame.estimated_size() * 10)
    get(cache, "a", lambda *_: frame)
    get(cache, "b", lambda *_: frame)
    clock[0] = 11
    get(cache, "c", lambda *_: frame)
    assert len(cache._cache) == 1
    assert cache.stats()["retained_bytes"] == frame.estimated_size()


def test_oversized_inflight_panel_is_still_shared(monkeypatch):
    import app.backtest.engine as engine

    frame = pl.DataFrame({"close": list(range(1000))})
    cache = PanelCache(max_bytes=1)
    entered, follower, release = (threading.Event() for _ in range(3))
    original_flight = engine._InFlight

    def flight():
        result = original_flight()
        original_wait = result.done.wait

        def wait(*args):
            follower.set()
            return original_wait(*args)

        result.done.wait = wait
        return result

    monkeypatch.setattr(engine, "_InFlight", flight)
    calls = []

    def compute(*_):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return frame

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(get, cache, "a", compute)
        try:
            assert entered.wait(2)
            second = pool.submit(get, cache, "a", compute)
            assert follower.wait(2)
        finally:
            release.set()
        assert first.result(timeout=2) is second.result(timeout=2) is frame
    assert calls == [True]
    assert cache.stats()["retained_bytes"] == 0
