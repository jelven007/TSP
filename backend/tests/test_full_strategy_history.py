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
