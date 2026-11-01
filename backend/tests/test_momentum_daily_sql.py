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
    archive_daily_backtest,
    previous_daily_state,
)


def test_baseline_daily_archive_restart_late_run_delete_and_tenant_isolation(env):
    _, user = register(env)
    _, other = register(env)
    ctx = env["identity"].context(user, None, "momentum-test")
    docs = TenantDocuments(env["db"], ctx)
    alien = TenantDocuments(env["db"], env["identity"].context(other, None, "alien"))
    params = {"initial_cash": 100000, "slippage": .0005, "stop_loss_pct": .06, "take_profit_pct": .06}
    state = {"as_of": "2026-08-24", "complete": True, "positions": [], "cash": 100000}
    day = {"as_of": "2026-08-24", "rows": [], "total": 0, "state": state,
           "sections": {"sell": [], "select": [], "buy": []}}
    result = {"config": {"params": params}, "strategy_info": {"id": "s02"}, "daily_results": [day]}
    assert archive_daily_backtest(docs, result, started_at=10) == 1
    assert previous_daily_state(docs, "s02", "2026-08-25") == state
    store = StrategyResults(docs)
    identity = ResultIdentity("s02", "", "", "{}", "{}", "null", "stock", "1m", daily_record=True)
    store.save(replace(identity, params_json='{"take_profit_pct":0.05}'), {**day, "total": 2}, started_at=20)
    store.save(identity, day, started_at=11)
    assert StrategyResults(TenantDocuments(env["db"], ctx)).read(identity, "2026-08-24")["result"]["total"] == 2
    assert len(store.list_history("s02")["records"]) == 1
    assert StrategyResults(alien).read(identity, "2026-08-24") is None
    assert archive_daily_backtest(docs, {**result, "config": {"params": {**params, "initial_cash": 200000}}},
                                  started_at=30) == 0
    with store.deleting_strategy("s02"):
        pass
    assert store.list_history("s02")["records"] == []
    with pytest.raises(AccessError):
        store.save(identity, day, started_at=31)
