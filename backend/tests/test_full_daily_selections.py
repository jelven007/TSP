"""Tenant post-close scheduling and saved-selection transport."""
# ruff: noqa: F401, F811 -- PostgreSQL fixture reuse.
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_full_daily_stock_pools import upgrade
from test_full_identity import env, full_cluster, register
from test_full_schedules import claim, own
from test_minute_signal_sections import CODE

from app.cell.context import AccessError
from app.multitenancy.scheduled_worker import TenantSchedules
from app.multitenancy.strategies import TenantStrategyEngine, definition_payload
from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy.engine import StrategyEngine

NOW = datetime(2026, 10, 30, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET = date(2026, 11, 2)


def source(documents, tmp_path):
    (tmp_path / "custom.py").write_text(CODE)
    definition = definition_payload(StrategyEngine([tmp_path]).get("custom_test"))
    documents.put("strategy_sources", "custom_test",
                  {"id": "custom_test", "source": "custom", "code": CODE, "definition": definition})


def selected():
    return {"as_of": str(TARGET), "source_date": "2026-10-30",
            "scheduled_for": NOW.isoformat(), "generated_at": NOW.isoformat(),
            "rows": [{"symbol": "600001.SH"}]}


def test_full_schedule_waits_retries_and_is_idempotent_and_tenant_private(env, tmp_path):
    from app.services.daily_selections import DailySelections
    _, documents = own(env)
    _, other = own(env)
    source(documents, tmp_path)
    calls = []
    def execute(task):
        calls.append(task)
        return {"selections": {"custom_test": selected()}, "errors": {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    lease = claim(env, documents)
    try:
        service.daily_selections(documents, {"settlement_days": []}, None, NOW, lease)
        assert calls == []
        assert DailySelections(documents).job(str(TARGET))["state"] == "waiting_data"
        ready = {"settlement_days": ["2026-10-30"], "public_release": {"id": "fixture"}}
        service.daily_selections(documents, ready, None, NOW, lease)
        service.daily_selections(documents, ready, None, NOW, lease)
        assert len(calls) == 1
        assert calls[0]["kind"] == "daily_selections"
        assert calls[0]["source_date"] == "2026-10-30"
        assert calls[0]["as_of"] == str(TARGET)
        assert DailySelections(documents).read("custom_test", TARGET)["rows"] == [{"symbol": "600001.SH"}]
        assert DailySelections(other).read("custom_test", TARGET) is None
        assert DailySelections(documents).job(str(TARGET))["state"] == "succeeded"
        identity = ResultIdentity("custom_test", "s02", "changed", "{}", "{}", "null", "stock", "1m")
        assert StrategyResults(documents).read(identity, str(TARGET))["result"]["selection_only"]
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()


@pytest.mark.parametrize("change", ["configuration", "lease", "deletion"])
def test_full_schedule_discards_stale_compute_output(env, tmp_path, change):
    from app.services.daily_selections import DailySelections
    _, documents = own(env)
    source(documents, tmp_path)
    lease = claim(env, documents)
    def execute(task):
        if change == "configuration":
            documents.put("strategy_overrides", "custom_test", {"params": {"changed": 1}})
        elif change == "lease":
            env["data"].execute(
                "UPDATE scheduler.work SET lease_until=now()-interval '1 second' WHERE tenant_id=%s",
                (documents.context.tenant_id,))
        else:
            with documents.transaction() as unit, StrategyResults(unit).deleting_strategy("custom_test"):
                unit.delete("strategy_sources", "custom_test")
        return {"selections": {"custom_test": selected()}, "errors": {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    try:
        with pytest.raises(AccessError):
            service.daily_selections(documents, {"settlement_days": ["2026-10-30"]}, None, NOW, lease)
        assert DailySelections(documents).read("custom_test", TARGET) is None
        if change != "lease":
            assert DailySelections(documents).job(str(TARGET))["state"] == "failed"
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()


def test_frozen_strategy_task_loads_exact_saved_selection_for_target_day(env, tmp_path):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_results import execution_snapshot
    _, documents = own(env)
    source(documents, tmp_path)
    key = ResultIdentity("custom_test", "s02", "v1", "{}", "{}", "null", "stock", "1m")
    DailySelections(documents).save(key, selected(), started_at=1)
    engine = TenantStrategyEngine(documents, StrategyEngine([]), SimpleNamespace())
    frozen = execution_snapshot(engine, documents)
    task = frozen.task("strategy", strategy_ids=["custom_test"], as_of=str(TARGET))
    assert task["selection_snapshots"]["custom_test"]["rows"] == [{"symbol": "600001.SH"}]
    assert engine.task("strategy", strategy_ids=["custom_test"], as_of="2026-11-03")["selection_snapshots"] == {}
