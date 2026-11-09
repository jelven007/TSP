"""Day projection isolation on real RLS-protected disposable PostgreSQL databases."""
# ruff: noqa: F811 — pytest fixture imports share the injected argument names.
import gzip
import json
from concurrent.futures import ThreadPoolExecutor

from test_full_identity import env, full_cluster, register  # noqa: F401 — pytest fixtures

from app.multitenancy.documents import TenantDocuments
from app.services.strategy_results import ResultIdentity, StrategyResults, clear_memory


def key():
    return ResultIdentity("day_sample", "样例", "v1", "{}", "{}", "null", "stock", "1m")


def result(symbol):
    rows = [{"symbol": symbol}]
    return {"as_of": "2026-06-22", "rows": rows, "total": 1, "section_mode": "signals",
            "sections": {"sell": [], "select": rows, "buy": rows}}


def payload(store):
    value = store.day_view("day_sample", "2026-06-22")
    return json.loads(gzip.decompress(value[1])) if value else None


def test_rls_cold_and_hot_views_and_uncommitted_rollback_are_isolated(env):
    _, first = register(env)
    _, second = register(env)
    own_docs = TenantDocuments(env["db"], env["identity"].context(first, None, "day-own"))
    other_docs = TenantDocuments(env["db"], env["identity"].context(second, None, "day-other"))
    own, other = StrategyResults(own_docs), StrategyResults(other_docs)
    own.save(key(), result("A"), started_at=1)
    assert payload(other) is None
    other.save(key(), result("B"), started_at=1)
    clear_memory()
    assert payload(own)["result"]["rows"] == [{"symbol": "A"}]
    assert payload(other)["result"]["rows"] == [{"symbol": "B"}]
    class RollbackError(Exception):
        pass
    try:
        with own_docs.transaction() as unit:
            temporary = StrategyResults(unit)
            temporary.save(key(), result("UNCOMMITTED"), started_at=2)
            assert payload(temporary)["result"]["rows"] == [{"symbol": "UNCOMMITTED"}]
            raise RollbackError()
    except RollbackError:
        pass
    assert payload(own)["result"]["rows"] == [{"symbol": "A"}]
    with own.deleting_strategy("day_sample"):
        pass
    assert payload(own) is None
    assert payload(other)["result"]["rows"] == [{"symbol": "B"}]


def test_concurrent_publication_returns_complete_matching_summary_and_sections(env):
    _, user = register(env)
    docs = TenantDocuments(env["db"], env["identity"].context(user, None, "day-race"))
    store = StrategyResults(docs)
    store.save(key(), result("0"), started_at=1)
    def writer():
        for index in range(1, 15):
            store.save(key(), result(str(index)), started_at=index + 1)
    def reader():
        for _ in range(30):
            value = payload(store)
            symbol = value["result"]["rows"][0]["symbol"]
            assert value["result"]["sections"]["buy"][0]["symbol"] == symbol
            assert symbol == str(value["summary"]["started_at"] - 1)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(writer), pool.submit(reader), pool.submit(reader)]
        for future in futures:
            future.result(timeout=30)
