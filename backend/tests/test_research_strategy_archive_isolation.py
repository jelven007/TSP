"""Frozen S02 research stays in research history, separate from live day results."""
import pytest

pytest.importorskip("psycopg")
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_research import Compute, payload, tenant

from app.multitenancy.research import ResearchWorker
from app.services.strategy_results import ResultIdentity, StrategyResults


@pytest.mark.parametrize("strategy_id", ["s02", "custom_mvxxcp88"],
                         ids=["frozen_retired_id", "current_stable_id"])
def test_completed_research_preserves_live_archive_and_retains_frozen_output(env, strategy_id):
    _, _, jobs = tenant(env)
    store = StrategyResults(jobs.documents)
    identity = ResultIdentity(strategy_id, "s02", "live", "{}", "{}", "null",
                              "stock", "1m", daily_record=True)
    day = "2026-08-24"
    live = {"as_of": day, "rows": [{"symbol": "600001.SH"}], "total": 1}
    store.save(identity, live, started_at=1)
    before = store.read(identity, day)
    # Historical input is retained as-is; this test submits no financial calculation.
    frozen = {
        "config": {"params": {"initial_cash": 100000, "slippage": .0005,
                             "stop_loss_pct": .06, "take_profit_pct": .06}},
        "strategy_info": {"id": strategy_id},
        "daily_results": [{"as_of": day, "rows": [], "total": 0,
                           "state": {"as_of": day, "complete": True,
                                     "cash": 100000, "positions": []}}],
    }
    row = jobs.submit(payload(jobs, config={"strategy_id": strategy_id}))
    worker = ResearchWorker(env["db"], Compute(frozen))
    try:
        worker.run(jobs.context, worker.claim(jobs.context))
    finally:
        worker.close()
    restored = jobs.get(row["id"])
    assert restored["state"] == "succeeded"
    assert {key: restored["result"][key] for key in frozen} == frozen
    assert restored["result"]["research"]["provenance"]["job_id"] == row["id"]
    assert store.read(identity, day) == before
    assert len(store.list_history(strategy_id)["records"]) == 1
