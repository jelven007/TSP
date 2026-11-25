"""Real SQL daily archive lifecycle and tenant isolation."""
from dataclasses import replace

import pytest

pytest.importorskip("psycopg")
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments
from app.services.strategy_results import (
    ResultIdentity,
    StrategyResults,
    previous_daily_state,
)


def test_daily_archive_restart_late_run_delete_and_tenant_isolation(env):
    _, user = register(env)
    _, other = register(env)
    ctx = env["identity"].context(user, None, "momentum-test")
    docs = TenantDocuments(env["db"], ctx)
    alien = TenantDocuments(env["db"], env["identity"].context(other, None, "alien"))
    state = {"as_of": "2026-08-24", "complete": True, "positions": [], "cash": 1000000}
    day = {"as_of": "2026-08-24", "rows": [], "total": 0, "state": state,
           "sections": {"sell": [], "select": [], "buy": []}}
    store = StrategyResults(docs)
    identity = ResultIdentity("custom_daily_archive", "", "", "{}", "{}", "null", "stock", "1m", daily_record=True)
    store.save(identity, day, started_at=10)
    assert previous_daily_state(docs, identity.strategy_id, "2026-08-25") == state
    store.save(replace(identity, params_json='{"take_profit_pct":0.05}'), {**day, "total": 2}, started_at=20)
    store.save(identity, day, started_at=11)
    assert StrategyResults(TenantDocuments(env["db"], ctx)).read(identity, "2026-08-24")["result"]["total"] == 2
    assert len(store.list_history(identity.strategy_id)["records"]) == 1
    assert StrategyResults(alien).read(identity, "2026-08-24") is None
    with store.deleting_strategy(identity.strategy_id):
        pass
    assert store.list_history(identity.strategy_id)["records"] == []
    with pytest.raises(AccessError):
        store.save(identity, day, started_at=31)
