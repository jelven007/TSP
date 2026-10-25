from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app.services.public_query_cache import PublicQueryCache


def test_public_release_roots_do_not_share_results_and_cache_is_bounded():
    cache = PublicQueryCache(limit=2)
    assert cache.get("data:daily", "/release/one", 30, lambda: {"rows": 1}) == {"rows": 1}
    assert cache.get("data:daily", "/release/two", 30, lambda: {"rows": 2}) == {"rows": 2}
    assert cache.get("data:daily", "/release/one", 30, lambda: {"rows": 3}) == {"rows": 1}
    cache.get("data:daily", "/release/three", 30, lambda: {"rows": 3})
    assert len(cache._entries) == 2
    assert cache.get("data:daily", "/release/two", 30, lambda: {"rows": 4}) == {"rows": 4}


def test_slow_read_cannot_reinstall_a_value_after_invalidation():
    cache = PublicQueryCache()
    entered, finish = Event(), Event()
    def old_read():
        entered.set()
        assert finish.wait(3)
        return "old"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cache.get, "overview", "root", 30, old_read)
        assert entered.wait(1)
        cache.invalidate({"overview"})
        finish.set()
        assert pending.result() == "old"
    assert cache.get("overview", "root", 30, lambda: "new") == "new"


def test_stage_invalidation_keeps_other_tables_and_storage():
    cache = PublicQueryCache()
    for namespace in ("data:daily", "data:minute", "data:quality", "storage"):
        cache.get(namespace, "root", 30, lambda: "before")
    cache.invalidate({"data:daily", "data:quality"})
    assert cache.get("data:daily", "root", 30, lambda: "after") == "after"
    assert cache.get("data:minute", "root", 30, lambda: "after") == "before"
    assert cache.get("storage", "root", 30, lambda: "after") == "before"
