"""Real PostgreSQL transaction and tenant isolation acceptance for result history."""
from dataclasses import replace

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments
from app.services.strategy_results import ResultIdentity, StrategyResults, clear_memory


def test_sql_history_is_tenant_scoped_and_ignores_configuration_collection_cap(env, monkeypatch):
    import app.multitenancy.documents as documents

    _, account = register(env)
    _, foreign = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(account, None, "history"))
    other = TenantDocuments(env["db"], env["identity"].context(foreign, None, "history-other"))
    key = ResultIdentity("sample", "样例", "v1", "{}", "{}", "null", "stock", "1d")
    monkeypatch.setattr(documents, "MAX_NAMESPACE_ITEMS", 1)
    store = StrategyResults(own)
    saved = store.save(key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "A"}]}, started_at=10)
    store.save(replace(key, strategy_version="v2"),
               {"as_of": "2026-10-22", "total": 0, "rows": []}, started_at=20)
    assert len(store.list_history("sample")["records"]) == 2
    assert StrategyResults(other).detail("sample", saved["record_id"]) is None
    assert StrategyResults(other).list_history("sample")["records"] == []
    clear_memory()
    assert StrategyResults(own).read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "A"}]
    assert "rows" not in store.summaries([key], "2026-10-22")["sample"]
    assert store.memberships([key], "2026-10-22", {"A"}) == {"A": ["sample"]}
    with pytest.raises(AccessError):
        StrategyResults(TenantDocuments(env["db"], replace(own.context, permissions=frozenset()))).read(
            key, "2026-10-22",
        )


def test_sql_history_write_rolls_back_with_domain_transaction(env):
    _, account = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(account, None, "history"))
    key = ResultIdentity("rollback", "回滚", "v1", "{}", "{}", "null", "stock", "1d")
    with pytest.raises(RuntimeError, match="undo"), own.transaction() as unit:
        StrategyResults(unit).save(key, {"as_of": "2026-10-22", "total": 0, "rows": []}, started_at=10)
        assert StrategyResults(unit).read(key, "2026-10-22")["result"]["total"] == 0
        raise RuntimeError("undo")
    assert StrategyResults(own).read(key, "2026-10-22") is None
    StrategyResults(own).save(key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "COMMITTED"}]},
                              started_at=20)
    assert StrategyResults(own).read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "COMMITTED"}]


@pytest.mark.parametrize("rows", [[], [{"symbol": "A", "score": 8}]])
def test_sql_duplicate_daily_result_and_metadata_rollback(env, rows):
    _, account = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(account, None, "dedup"))
    key = ResultIdentity("sample", "样例", "v1", "{}", "{}", "null", "stock", "1d")
    store = StrategyResults(own)
    result = {"as_of": "2026-10-22", "total": len(rows), "rows": rows, "elapsed_ms": 10}
    first = store.save(key, result, started_at=10, input_generation={"market": 1})
    store.read(key, result["as_of"])
    with pytest.raises(RuntimeError, match="undo"), own.transaction() as unit:
        pending = StrategyResults(unit).save(
            key, {**result, "elapsed_ms": 20}, started_at=30, input_generation={"market": 2},
        )
        assert pending["result"] == first["result"]
        assert pending["summary"]["started_at"] == 30
        assert StrategyResults(unit).read(key, result["as_of"]) == pending
        raise RuntimeError("undo")
    assert store.read(key, result["as_of"]) == first
    repeated = store.save(key, {**result, "elapsed_ms": 20, "computed_at": 999},
                          started_at=30, input_generation={"market": 2})
    assert repeated["result"] == first["result"]
    assert repeated["_cache_token"] == first["_cache_token"]
    assert repeated["summary"]["computed_at"] == first["summary"]["computed_at"]
    assert repeated["summary"]["input_generation"] == {"market": 2}
    assert store.read(key, result["as_of"]) == repeated
    before = own.record("strategy_result_history", first["record_id"])
    assert store.save(key, result, started_at=30, input_generation={"market": 2}) == repeated
    assert store.save(key, {**result, "rows": [{"symbol": "OLD"}]}, started_at=20) == repeated
    assert own.record("strategy_result_history", first["record_id"]) == before
    clear_memory()
    assert StrategyResults(own).read(key, result["as_of"]) == repeated
    assert len(store.list_history("sample")["records"]) == 1


def test_sql_history_filters_delete_isolation_and_rollback(env):
    _, account = register(env)
    _, foreign = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(account, None, "history"))
    other = TenantDocuments(env["db"], env["identity"].context(foreign, None, "foreign"))
    key = ResultIdentity("sample", "旧名称_100%", "Version-ABC", "{}", "{}", "null", "stock", "1m")
    store = StrategyResults(own)
    for day in ("2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"):
        store.save(key, {"as_of": day, "rows": [{"symbol": "OLD"}]}, started_at=1)
        store.save(replace(key, timeframe="1d"),
                   {"as_of": day, "rows": []}, started_at=1)
    filters = dict(q="旧名称_100%", start_date="2026-10-20", end_date="2026-10-22",
                   asset_type="stock", timeframe="1m", limit=2)
    page = store.list_history("sample", **filters)
    assert [r["as_of"] for r in page["records"]] == ["2026-10-22", "2026-10-21"]
    second = store.list_history("sample", cursor=page["next_cursor"], **filters)
    assert [r["as_of"] for r in second["records"]] == ["2026-10-20"]
    assert second["next_cursor"] is None
    assert len(store.list_history("sample", q="version-abc")["records"]) == 8
    record_id = key.record_id("2026-10-22")
    assert not StrategyResults(other).delete("sample", record_id)
    readonly = TenantDocuments(env["db"], replace(own.context, permissions=frozenset({"business:read"})))
    with pytest.raises(AccessError) as forbidden:
        StrategyResults(readonly).delete("sample", record_id)
    assert forbidden.value.status == 403
    store.read(key, "2026-10-22")
    with pytest.raises(RuntimeError, match="undo"), own.transaction() as unit:
        assert StrategyResults(unit).delete("sample", record_id)
        assert StrategyResults(unit).read(key, "2026-10-22") is None
        raise RuntimeError("undo")
    assert store.read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "OLD"}]
    # External process leaves this process's hot cache untouched; revision resets on insert.
    with own._transaction(write=True) as conn:
        conn.execute("DELETE FROM business.documents WHERE namespace=%s AND id=%s",
                     ("strategy_result_history", record_id))
    store.save(key, {"as_of": "2026-10-22", "rows": [{"symbol": "NEW"}]}, started_at=2)
    assert store.read(key, "2026-10-22")["result"]["rows"] == [{"symbol": "NEW"}]
    assert store.delete("sample", record_id)
    assert store.read(key, "2026-10-22") is None
    assert len(store.list_history("sample")["records"]) == 7


def test_sql_history_http_delete_requires_write_permission_and_same_origin(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import screener
    from app.multitenancy import access

    token, account = register(env)
    foreign_token, _ = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(account, None, "http"))
    key = ResultIdentity("sample", "样例", "v1", "{}", "{}", "null", "stock", "1d")
    store = StrategyResults(own)
    store.save(key, {"as_of": "2026-10-22", "rows": []}, started_at=1)
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    app.include_router(screener.router)
    url = f"/api/screener/result-history/sample/{key.record_id('2026-10-22')}"
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", foreign_token)
        assert client.delete(url).status_code == 404
        client.cookies.set("tf_session", token)
        assert client.delete(url, headers={"Origin": "https://foreign.test"}).status_code == 403
        original = env["identity"].current_user
        monkeypatch.setattr(env["identity"], "current_user", lambda *a, **kw: {
            **original(*a, **kw), "permissions": ["business:read"],
        })
        assert client.get("/api/screener/result-history/sample").status_code == 200
        assert client.delete(url).status_code == 403
        assert store.read(key, "2026-10-22") is not None
        monkeypatch.setattr(env["identity"], "current_user", original)
        assert client.delete(url).status_code == 200
        assert client.get(url).status_code == 404
