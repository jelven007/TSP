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

NOW = datetime(2026, 10, 30, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET = date(2026, 11, 2)


def test_saved_suggestions_are_tenant_private_transactional_and_frozen(env):
    from test_minute_signal_sections import signal_result

    from app.services.daily_selections import DailySelections
    _, documents = own(env)
    _, other = own(env)
    identity = ResultIdentity("custom_test", "s02", "v1", "{}", "{}", "null", "stock", "1m")
    store = StrategyResults(documents)
    prior = store.save(identity, {
        **signal_result("2026-10-30", [{"symbol": "600001.SH", "close": 9.}]),
        "price_suggestion_reference": {"reference_date": "2026-10-29", "closes": {}},
    }, started_at=1)
    assert prior["result"]["sections"]["buy"][0]["suggested_lots_100k"] == 111
    snapshot = {**selected(), "price_suggestion_reference": {
        "reference_date": "2026-10-30", "closes": {"600001.SH": 10.25},
    }}
    with pytest.raises(ValueError), documents.transaction() as unit:
        DailySelections(unit).save(identity, snapshot, started_at=2)
        raise ValueError("rollback")
    assert DailySelections(documents).read("custom_test", TARGET) is None
    saved = DailySelections(documents).save(identity, snapshot, started_at=2)
    assert saved["result"]["sections"]["sell"][0]["suggested_sell_price"] == {
        "lower": 9.64, "upper": 10.87, "reference_date": "2026-10-30", "reference_close": 10.25,
    }
    assert "price_suggestion_reference" not in saved["result"]
    assert "price_suggestion_reference" not in saved["result"]["selection_snapshot"]
    assert DailySelections(other).read("custom_test", TARGET) is None
    assert store.detail("custom_test", prior["record_id"]) == prior


def source(documents, tmp_path):
    (tmp_path / "custom.py").write_text(CODE)
    definition = definition_payload(StrategyEngine([tmp_path]).get("custom_test"))
    documents.put("strategy_sources", "custom_test",
                  {"id": "custom_test", "source": "custom", "code": CODE, "definition": definition})
    from app.services.strategy_monitor_tasks import set_monitoring
    set_monitoring(documents, "custom_test", "Test", True)


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


@pytest.mark.parametrize("clock", [(20, 0), (20, 47), (21, 59)])
def test_all_custom_strategies_recover_window_start_without_archive(env, tmp_path, clock):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_monitor_tasks import set_monitoring, task_receipt
    _, documents = own(env)
    source(documents, tmp_path)
    # Different arbitrary IDs prove that the scheduler has no S03-specific gate.
    for sid in ("custom_alpha", "custom_beta"):
        code = CODE.replace("custom_test", sid)
        (tmp_path / (sid + ".py")).write_text(code)
        definition = definition_payload(StrategyEngine([tmp_path]).get(sid))
        documents.put("strategy_sources", sid, {
            "id": sid, "source": "custom", "code": code, "definition": definition})
        set_monitoring(documents, sid, sid, True)
    ids = ["custom_alpha", "custom_beta", "custom_test"]
    for sid in ids:
        task_receipt(documents, sid, NOW.date(), {
            "tasks": {"select": {"state": "success", "terminal": True}}})
    calls = []
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(
        execute=lambda task: calls.append(task["strategy_ids"]) or {
            "selections": {sid: {**selected(), "rows": []} for sid in task["strategy_ids"]},
            "errors": {}}))
    lease = claim(env, documents)
    try:
        now = NOW.replace(hour=clock[0], minute=clock[1])
        ready = {"settlement_days": ["2026-10-30"]}
        service.daily_selections(documents, ready, None, now, lease)
        service.daily_selections(documents, ready, None, now, lease)
        assert len(calls) == 1
        assert sorted(calls[0]) == ids
        assert all(DailySelections(documents).read(sid, TARGET)["rows"] == [] for sid in ids)
        assert all(task_receipt(documents, sid, NOW.date())["tasks"]["select"]["state"] == "success"
                   for sid in ids)
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()

def test_plain_custom_without_signal_sections_still_gets_the_nightly_job(env, tmp_path):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_monitor_tasks import set_monitoring
    _, documents = own(env)
    code = ('import polars as pl\n'
            'META = {"id": "custom_plain", "name": "普通自定义", "asset_types": ["stock"], '
            '"timeframes": ["1d"]}\n'
            'def filter(df, params):\n    return pl.lit(False)\n')
    path = tmp_path / "plain.py"
    path.write_text(code)
    definition = definition_payload(StrategyEngine([tmp_path]).get("custom_plain"))
    documents.put("strategy_sources", "custom_plain", {
        "id": "custom_plain", "source": "custom", "code": code, "definition": definition})
    set_monitoring(documents, "custom_plain", "普通自定义", True)
    calls = []
    def execute(task):
        calls.append(task)
        return {"selections": {"custom_plain": selected()}, "errors": {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    lease = claim(env, documents)
    try:
        service.daily_selections(documents, {"settlement_days": ["2026-10-30"],
            "public_release": {"id": "fixture"}}, None, NOW, lease)
        assert len(calls) == 1
        assert calls[0]["strategy_ids"] == ["custom_plain"]
        assert DailySelections(documents).read("custom_plain", TARGET)["rows"] == selected()["rows"]
        assert StrategyResults(documents).day_view("custom_plain", str(TARGET), timeframe="1d") is not None
        assert StrategyResults(documents).day_view("custom_plain", str(TARGET), timeframe="1m") is None
        from app.services.strategy_results import capture_identity
        identity = capture_identity(StrategyEngine([tmp_path]), documents, "custom_plain", timeframe="1d")
        store = StrategyResults(documents)
        assert store.read(identity, str(TARGET))["result"]["sections"]["select"] == selected()["rows"]
        assert "custom_plain" in store.summaries([identity], str(TARGET))
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()

@pytest.mark.parametrize("patch", [
    {"rows": None}, {"errors": ["failed evidence"]},
    {"coverage": {"scope_total": 10, "available_daily": 0}},
])
def test_abnormal_response_is_retried_without_discarding_other_strategy(env, tmp_path, patch):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_monitor_tasks import set_monitoring
    _, documents = own(env)
    source(documents, tmp_path)
    code = CODE.replace("custom_test", "custom_other")
    (tmp_path / "other.py").write_text(code)
    definition = definition_payload(StrategyEngine([tmp_path]).get("custom_other"))
    documents.put("strategy_sources", "custom_other", {
        "id": "custom_other", "source": "custom", "code": code, "definition": definition})
    set_monitoring(documents, "custom_other", "Other", True)
    calls = []
    def execute(task):
        calls.append(task["strategy_ids"])
        return {"selections": {sid: {**selected(), **(patch if len(calls) == 1
            and sid == "custom_test" else {})} for sid in task["strategy_ids"]}, "errors": {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    lease = claim(env, documents)
    try:
        ready = {"settlement_days": ["2026-10-30"]}
        service.daily_selections(documents, ready, None, NOW, lease)
        store = DailySelections(documents)
        assert store.read("custom_other", TARGET) is not None
        assert store.read("custom_test", TARGET) is None
        assert "custom_test" in store.job(str(TARGET))["errors"]
        service.daily_selections(documents, ready, None, NOW, lease)
        assert calls[1] == ["custom_test"]
        assert store.read("custom_test", TARGET) is not None
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()


@pytest.mark.parametrize("change", ["configuration", "lease", "deletion", "cancel", "window"])
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
        elif change == "cancel":
            from app.services.strategy_monitor_tasks import set_monitoring
            set_monitoring(documents, "custom_test", "Test", False)
        elif change == "window":
            service.selection_now = lambda: NOW.replace(hour=22, second=1)
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


@pytest.mark.parametrize("hour,enabled", [(19, True), (23, True), (20, False)])
def test_nightly_automatic_work_requires_monitor_and_current_window(env, tmp_path, hour, enabled):
    from app.services.daily_selections import DailySelections
    from app.services.strategy_monitor_tasks import set_monitoring
    _, documents = own(env)
    source(documents, tmp_path)
    if not enabled:
        set_monitoring(documents, "custom_test", "Test", False)
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(
        execute=lambda _: pytest.fail("unmonitored or out-of-window computation")))
    lease = claim(env, documents)
    try:
        service.daily_selections(documents, {"settlement_days": ["2026-10-30"]},
                                 None, NOW.replace(hour=hour), lease)
        assert DailySelections(documents).read("custom_test", TARGET) is None
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


def test_nightly_mainline_waits_until_platform_repair_finishes(env, tmp_path):
    from app.services.daily_selections import DailySelections
    _, documents = own(env)
    source(documents, tmp_path)
    store = DailySelections(documents)
    request = {"owner": documents.context.tenant_id, "strategy_id": "custom_test", "generation": 1}
    store.job(str(TARGET), {"source_date": "2026-10-30", "as_of": str(TARGET), "attempts": 1,
        "errors": {"custom_test": "source data missing"}, "repairs": {
            "custom_test": {"state": "running", "request": request}}})
    calls, ready = [], {"value": False}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(
        execute=lambda task: calls.append(task) or {"selections": {"custom_test": selected()}, "errors": {}}))
    service.market = SimpleNamespace(monitor_repair=lambda _: {
        "state": "succeeded" if ready["value"] else "running"})
    lease = claim(env, documents)
    try:
        snapshot = {"settlement_days": ["2026-10-30"]}
        service.daily_selections(documents, snapshot, None, NOW, lease)
        assert calls == []
        assert store.job(str(TARGET))["attempts"] == 1
        ready["value"] = True
        service.daily_selections(documents, snapshot, None, NOW, lease)
        assert len(calls) == 1
        assert store.read("custom_test", TARGET) is not None
    finally:
        from app.cell.scheduler import Scheduler
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()
