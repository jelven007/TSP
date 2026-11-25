"""S03's nightly archive must replace only its fixed watchlist group."""
# ruff: noqa: F401, F811 -- Shared real PostgreSQL fixtures.
from types import SimpleNamespace

import pytest
from test_full_daily_selections import NOW, TARGET, s03_source, save_s02, source
from test_full_daily_stock_pools import upgrade
from test_full_identity import env, full_cluster
from test_full_schedules import claim, own

from app.multitenancy.documents import TenantDocuments
from app.multitenancy.scheduled_worker import TenantSchedules
from app.multitenancy.watchlist import Watchlist
from app.services.daily_selections import DailySelections

SID = "custom_mvxxcs03"
RUN = NOW.replace(hour=22, minute=6)
READY = {"settlement_days": [str(NOW.date())]}


def prepared(env, tmp_path):
    _, documents = own(env)
    source(documents, tmp_path)
    s03_source(documents, tmp_path)
    save_s02(documents, rows=[{"symbol": "600030.SH"}, {"symbol": "600001.SH"}])
    watchlist = Watchlist(env["db"], documents.context)
    _, fixed = watchlist.create_group("s03", "orange")
    _, other = watchlist.create_group("保留组")
    watchlist.add("600010.SH", "旧组独占备注", group_id=fixed["id"])
    watchlist.set_strategies("600010.SH", ["old_tag"])
    watchlist.add_batch(["600020.SH"], "共享旧股", group_ids=[fixed["id"], other["id"]])
    watchlist.add("600030.SH", "共享新股备注", group_id=other["id"])
    watchlist.set_strategies("600030.SH", ["kept_tag"])
    watchlist.add("600050.SH", "未分组保留")
    return documents, watchlist, fixed, other


def selected(empty=False):
    rows = [] if empty else [
        {"symbol": "600030.SH", "score": 70., "selection_rank": 1},
        {"symbol": "600001.SH", "score": 60., "selection_rank": 2},
    ]
    return {
        "as_of": str(TARGET), "source_date": str(NOW.date()), "rows": rows,
        "scheduled_for": NOW.replace(hour=22).isoformat(),
        "generated_at": NOW.replace(hour=22, minute=5).isoformat(),
        "scoring_policy": "s03_five_dimensions_v1",
        "coverage": {"candidate_total": len(rows), "scorable_total": len(rows),
                     "selected_total": len(rows), "complete": True},
    }


@pytest.mark.parametrize("empty", [False, True])
def test_nightly_success_replaces_members_preserves_other_data_and_is_idempotent(env, tmp_path, empty):
    documents, watchlist, fixed, other = prepared(env, tmp_path)
    _, unrelated = own(env)
    calls = []
    def execute(task):
        calls.append(task)
        return {"selections": {SID: selected(empty)}, "errors": {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    lease = claim(env, documents)
    try:
        service.daily_selections(documents, READY, None, RUN, lease)
        rows = watchlist.list_symbols()
        expected = [] if empty else ["600030.SH", "600001.SH"]
        assert [r["symbol"] for r in rows if fixed["id"] in r["group_ids"]] == expected
        by_symbol = {r["symbol"]: r for r in rows}
        assert "600010.SH" not in by_symbol
        assert by_symbol["600020.SH"]["group_ids"] == [other["id"]]
        assert by_symbol["600020.SH"]["note"] == "共享旧股"
        assert by_symbol["600030.SH"]["note"] == "共享新股备注"
        assert by_symbol["600030.SH"]["strategy_ids"] == ["kept_tag"]
        assert by_symbol["600050.SH"]["group_ids"] == []
        assert watchlist.list_groups()[0]["id"] == fixed["id"]
        assert watchlist.list_groups()[0]["color"] == "orange"
        assert Watchlist(env["db"], unrelated.context).list_symbols() == []
        assert DailySelections(documents).read(SID, TARGET)["rows"] == selected(empty)["rows"]
        revision = watchlist.revision()
        service.daily_selections(documents, READY, None, RUN.replace(minute=7), lease)
        assert watchlist.list_symbols() == rows
        assert watchlist.revision() == revision
        assert len(calls) == 1
        with documents.transaction(write=False) as unit:
            events = unit.connection.execute(
                "SELECT payload FROM business.full_events WHERE kind='strategy_results_updated'").fetchall()
        assert [r["payload"] for r in events if r["payload"].get("watchlist_changed")] == [
            {"as_of": str(TARGET), "section": "select", "strategy_ids": [SID], "watchlist_changed": True}]
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()


def test_group_write_failure_rolls_back_archive_and_group_then_retries(env, tmp_path, monkeypatch):
    documents, watchlist, fixed, _other = prepared(env, tmp_path)
    before = watchlist.list_symbols()
    revision = watchlist.revision()
    original = TenantDocuments.put
    broken = {"value": True}
    def put(self, namespace, item_id, value, **kwargs):
        if namespace == "s03_watchlist_sync" and broken["value"]:
            raise RuntimeError("fixture sync write failure")
        return original(self, namespace, item_id, value, **kwargs)
    monkeypatch.setattr(TenantDocuments, "put", put)
    calls = []
    def execute(task):
        calls.append(task)
        return {"selections": {SID: selected()}, "errors": {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    lease = claim(env, documents)
    try:
        service.daily_selections(documents, READY, None, RUN, lease)
        assert watchlist.list_symbols() == before
        assert watchlist.revision() == revision
        assert DailySelections(documents).read(SID, TARGET) is None
        with documents.transaction(write=False) as unit:
            assert not unit.connection.execute(
                "SELECT 1 FROM business.full_events WHERE payload->>'watchlist_changed'='true'").fetchone()
        job = DailySelections(documents).job(str(TARGET) + ":s03")
        assert job["state"] == "failed"
        assert "自选" in job["errors"][SID]
        broken["value"] = False
        service.daily_selections(documents, READY, None, RUN.replace(minute=7), lease)
        assert [r["symbol"] for r in watchlist.list_symbols()
                if fixed["id"] in r["group_ids"]] == ["600030.SH", "600001.SH"]
        assert len(calls) == 2
        assert calls[0]["source_selection"] == calls[1]["source_selection"]
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()


def test_saved_archive_recovers_sync_without_recomputing_and_deadline_does_not_clear(env, tmp_path):
    from app.services.strategy_results import ResultIdentity
    documents, watchlist, fixed, _ = prepared(env, tmp_path)
    identity = ResultIdentity(SID, "s03", "v1", "{}", "{}", "null", "stock", "1m")
    DailySelections(documents).save(identity, selected(), started_at=1)
    before = watchlist.list_symbols()
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(
        execute=lambda task: pytest.fail("saved scoring recomputed")))
    lease = claim(env, documents)
    try:
        service.daily_selections(documents, READY, None, RUN.replace(hour=23, second=1), lease)
        assert watchlist.list_symbols() == before
        service.daily_selections(documents, READY, None, RUN, lease)
        assert [r["symbol"] for r in watchlist.list_symbols()
                if fixed["id"] in r["group_ids"]] == ["600030.SH", "600001.SH"]
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()
