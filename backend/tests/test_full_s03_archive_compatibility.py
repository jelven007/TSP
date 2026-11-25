"""Real tenant schedules cannot skip scoring because a legacy archive is readable."""
# ruff: noqa: F401, F811 -- PostgreSQL fixture reuse.
from types import SimpleNamespace

import pytest
from test_full_daily_stock_pools import upgrade
from test_full_identity import env, full_cluster
from test_full_s03_watchlist_sync import READY, RUN, SID, prepared, selected
from test_full_schedules import claim, own

from app.multitenancy.scheduled_worker import TenantSchedules
from app.services.daily_selections import DailySelections
from app.services.strategy_results import ResultIdentity


@pytest.mark.parametrize("empty", [False, True])
def test_legacy_archive_is_preserved_while_new_policy_is_scheduled_once(env, tmp_path, empty):
    documents, watchlist, fixed, _other = prepared(env, tmp_path)
    _, other = own(env)
    store = DailySelections(documents)
    identity = ResultIdentity(SID, "s03", "v1", "{}", "{}", "null", "stock", "1m")
    old_snapshot = {**selected(), "scoring_policy": None,
                    "rows": [{"symbol": f"LEGACY{i}"} for i in range(80)]}
    old = store.save(identity, old_snapshot, started_at=1)
    assert store.read(SID, old_snapshot["as_of"]) is None
    calls = []
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(
        execute=lambda task: calls.append(task) or {
            "selections": {SID: selected(empty)}, "errors": {}}))
    lease = claim(env, documents)
    try:
        service.daily_selections(documents, READY, None, RUN, lease)
        current = store.read(SID, old_snapshot["as_of"])
        assert current["scoring_policy"] == "s03_five_dimensions_v1"
        assert current["rows"] == selected(empty)["rows"]
        assert store.results.detail(SID, old["record_id"]) == old
        assert DailySelections(other).read(SID, old_snapshot["as_of"]) is None
        assert [r["symbol"] for r in watchlist.list_symbols() if fixed["id"] in r["group_ids"]] == [
            r["symbol"] for r in selected(empty)["rows"]]
        service.daily_selections(documents, READY, None, RUN, lease)
        assert len(calls) == 1
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()
