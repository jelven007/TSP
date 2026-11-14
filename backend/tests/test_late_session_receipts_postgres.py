"""First receipts persist through real RLS transactions and rollback."""
# ruff: noqa: F811 -- injected fixtures are imported from the full-product suite.
from dataclasses import replace

import test_late_session_progress_archive as regression
from test_full_identity import env, full_cluster, register  # noqa: F401

from app.multitenancy.documents import TenantDocuments
from app.services.strategy_results import ResultIdentity, StrategyResults, clear_memory


def test_postgres_first_receipt_summary_change_isolation_and_rollback(env):
    _, user = register(env)
    _, other = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(user, None, "receipt-test"))
    other_docs = TenantDocuments(env["db"], env["identity"].context(other, None, "receipt-other"))
    identity = ResultIdentity("receipt_probe", "回执测试", "v1", "{}", "{}", "null",
                              "stock", "1m", daily_record=True)
    store = StrategyResults(own)
    regression.test_execution_clocks_do_not_overwrite_identical_receipt((store, identity))
    identity = replace(identity, strategy_id="receipt_metrics")
    regression.test_metric_change_keeps_complete_first_receipt((store, identity))
    clear_memory()
    saved = store.read(identity, "2026-11-13")
    assert regression.step(saved)["first_receipt"]["passed"] == 541
    assert StrategyResults(other_docs).read(identity, "2026-11-13") is None
    class RollbackError(Exception):
        pass
    try:
        with own.transaction(write=True) as unit:
            changed = StrategyResults(unit).save(identity, regression.sample("14:54:00", 700), started_at=4)
            assert regression.step(changed)["passed"] == 700
            assert regression.step(changed)["first_receipt"] == regression.step(saved)["first_receipt"]
            raise RollbackError()
    except RollbackError:
        pass
    clear_memory()
    assert store.read(identity, "2026-11-13")["result"] == saved["result"]
    identity = replace(identity, strategy_id="receipt_failure")
    regression.test_failed_and_out_of_order_run_cannot_replace_first((store, identity))
