"""Strategy cancellation fences old work and disables legacy recovery atomically."""
# ruff: noqa: F401,F811 -- isolated PostgreSQL fixture reuse.
from types import SimpleNamespace

import pytest
from test_critical_strategy_schedule import configure
from test_full_daily_stock_pools import upgrade
from test_full_identity import env, full_cluster
from test_full_runtime import owned
from test_minute_signal_sections import scan

from app.api import monitor_rules as api
from app.multitenancy.critical_schedule import reconcile
from app.multitenancy.monitor_rules import Rules
from app.multitenancy.runtime import fingerprint
from app.strategy.monitor_rules import normalize


def test_cancel_custom_disables_all_rules_recovery_and_changes_commit_fingerprint(owned, monkeypatch):
    sid = configure(owned)
    reconcile(owned.worker, owned.documents)
    assert not owned.documents.get("critical_strategy_schedule", sid)["enabled"]
    Rules(owned.documents).save_one(normalize({
        "id": "ordinary_duplicate", "name": "S02", "type": "strategy", "scope": "all",
        "strategy_id": sid,
    }))
    monkeypatch.setattr(api, "_data_dir", lambda _: owned.documents)
    from app.api import strategy as strategy_api
    monkeypatch.setattr(strategy_api, "_get_engine", lambda _: SimpleNamespace(
        get=lambda _: SimpleNamespace(source="custom", meta={"name": "S02"})))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=owned.repo)))
    with owned.documents.transaction(write=False) as unit:
        before = fingerprint(unit.connection, include_state=False)
    cancelled = api.set_strategy_monitoring(sid, api.StrategyMonitoringModel(enabled=False), request)
    assert cancelled["enabled"] is False
    assert not any(rule["enabled"] for rule in Rules(owned.documents).load_all())
    assert owned.documents.get("critical_strategy_schedule", sid)["enabled"] is False
    reconcile(owned.worker, owned.documents)
    assert not any(rule["enabled"] for rule in Rules(owned.documents).load_all())
    assert owned.other.get("strategy_monitoring", sid) is None
    with owned.documents.transaction(write=False) as unit:
        assert fingerprint(unit.connection, include_state=False) != before
    enabled = api.set_strategy_monitoring(sid, api.StrategyMonitoringModel(enabled=True), request)
    assert enabled["enabled"]
    assert sum(rule["enabled"] for rule in Rules(owned.documents).load_all()) == 1
    with owned.documents.transaction(write=False) as unit:
        assert fingerprint(unit.connection, include_state=False) != before


def test_failed_switch_rolls_back_both_lifecycle_and_rules(owned, monkeypatch):
    from app.services.strategy_monitor_tasks import set_monitoring
    original = Rules.save_one
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("rollback")
    monkeypatch.setattr(Rules, "save_one", fail)
    with pytest.raises(RuntimeError, match="rollback"):
        set_monitoring(owned.documents, "custom_test", "Test", True)
    assert owned.documents.get("strategy_monitoring", "custom_test") is None
    assert Rules(owned.documents).load_all() == []


def test_cancelled_strategy_cannot_run_via_reenabled_legacy_rule(owned, tmp_path):
    from test_full_daily_selections import source

    from app.services.strategy_monitor_tasks import set_monitoring
    source(owned.documents, tmp_path)
    rule = Rules(owned.documents).load_all()[0]
    set_monitoring(owned.documents, "custom_test", "Test", False)
    Rules(owned.documents).save_one({**rule, "enabled": True})
    _, _, payload, _ = owned.worker.prepare(
        owned.documents.context.tenant_id, owned.snapshot(), None)
    assert not payload["rules"]
    assert not payload["strategy_task_ids"]


def test_deleted_buy_receipt_removes_forward_roster_on_postgres(owned):
    from test_strategy_sell_rosters import bought, identity

    from app.services.strategy_monitor_tasks import sell_roster
    from app.services.strategy_results import StrategyResults
    store = StrategyResults(owned.documents)
    saved = store.save(identity(), bought([{"symbol": "600001.SH"}]), started_at=1)
    store.delete("custom_test", saved["record_id"])
    assert sell_roster(owned.documents, "custom_test", "2026-11-02") is None
    assert store.day_view("custom_test", "2026-11-02") is None


@pytest.mark.parametrize("rows", [[], [{"symbol": "600001.SH", "close": 10.}]])
def test_next_sell_roster_and_rollback_are_private_on_postgres(owned, rows):
    from test_strategy_sell_rosters import bought, identity

    from app.services.strategy_monitor_tasks import sell_roster
    from app.services.strategy_results import StrategyResults
    with pytest.raises(RuntimeError), owned.documents.transaction() as unit:
        StrategyResults(unit).save(identity(), bought(rows), started_at=1)
        assert sell_roster(unit, "custom_test", "2026-11-02")["rows"] == [
            {**r, "entry_date": "2026-10-30",
             "entry_record_id": identity().record_id("2026-10-30")} for r in rows]
        raise RuntimeError("rollback")
    assert sell_roster(owned.documents, "custom_test", "2026-11-02") is None
    StrategyResults(owned.documents).save(identity(), bought(rows), started_at=2)
    assert sell_roster(owned.documents, "custom_test", "2026-11-02") is not None
    assert sell_roster(owned.other, "custom_test", "2026-11-02") is None


def test_active_sell_task_runs_without_fresh_quote_and_archives_independent_result(owned, tmp_path):
    import gzip
    import json
    from datetime import datetime, time

    from test_full_daily_selections import source
    from test_strategy_sell_rosters import bought, identity
    from test_strategy_sell_tasks import SIGNAL

    from app.market_time import CN_TZ, cn_today
    from app.services.strategy_results import StrategyResults
    from app.strategy.basic_filter import previous_session
    source(owned.documents, tmp_path)
    owned.documents.put("custom_signals", "morning", SIGNAL)
    owned.documents.put("strategy_overrides", "custom_test", {"exit_signals": ["csgi_morning"]})
    prior = str(previous_session(cn_today()))
    StrategyResults(owned.documents).save(identity(), {
        **bought([{"symbol": "600001.SH"}]), "as_of": prior,
    }, started_at=1)
    owned.clock["now"] = datetime.combine(cn_today(), time(10, 50), CN_TZ).timestamp()
    calls = []
    def execute(payload, **kwargs):
        calls.append(payload)
        assert payload["sell_rosters"]["custom_test"]["source_date"] == prior
        daily = {"as_of": str(cn_today()), "section_mode": "signals", "rows": [], "total": 0,
                 "entry_filter_error": "等待入场", "section_errors": {"buy": "等待入场"},
                 "section_statuses": {"buy": "waiting", "sell": "success"},
                 "sections": {"sell": payload["sell_rosters"]["custom_test"]["rows"], "buy": [], "select": []},
                 "exit_signal_hits": [{"symbol": "600001.SH", "signal_roster": True, "reason": "stop_time"}],
                 "monitor_tasks": {"sell": {"state": "success", "count": 1}}}
        return {"events": [], "execution_events": [], "state": {},
                "results": {"custom_test": daily}}
    owned.worker.executor = SimpleNamespace(execute=execute)
    owned.worker.evaluate(owned.documents.context.tenant_id,
                          owned.snapshot(status={"is_trading_hours": False}))
    assert len(calls) == 1
    view = StrategyResults(owned.documents).day_view("custom_test", str(cn_today()))
    result = json.loads(gzip.decompress(view[1]))["result"]
    assert result["exit_signal_hits"][0]["reason"] == "stop_time"
    assert result["section_statuses"]["buy"] == "waiting"


def test_completed_custom_receipt_survives_global_wait_and_quote_ttl(owned, monkeypatch):
    from app.api import screener
    from app.market_time import cn_today
    from app.services.strategy_monitor_state import CONTRACT
    from app.services.strategy_results import ResultIdentity
    day = str(cn_today())
    sid = "custom_test"
    monkeypatch.setattr(screener, "_private_data_dir", lambda _: owned.documents)
    with owned.documents.transaction() as unit:
        stamp = fingerprint(unit.connection, include_state=False)
        unit.put("strategy_execution_status", "default", {
            "state": "waiting", "as_of": day, "strategy_ids": [sid], "fingerprint": stamp})
        unit.put("strategy_execution_status", sid, {
            "state": "success", "as_of": day, "fingerprint": stamp, "runtime_identity": "def"})
    daily = {"as_of": day, "runtime_identity": "def", "computed_at": 100,
             "monitor_contract": CONTRACT,
             "monitor_tasks": {"buy": {"state": "rejected", "terminal": True}},
             "section_statuses": {"buy": "rejected"},
             "execution_status": {"state": "success"}, "rows": [], "total": 0}
    identity = ResultIdentity(sid, sid, "def", "{}", "{}", "null", "stock", "1m")
    result = screener._with_execution_status(SimpleNamespace(), {sid: daily}, day, identities=[identity])
    assert result[sid] == daily


def test_unrelated_monitor_failure_cannot_erase_completed_custom_phase(owned, tmp_path, monkeypatch):
    from datetime import datetime, time

    from test_full_daily_selections import source

    from app.market_time import CN_TZ, cn_today
    from app.multitenancy import monitor_compute
    from app.services.strategy_monitor_tasks import task_receipt
    source(owned.documents, tmp_path)
    Rules(owned.documents).save_one(__import__(
        "test_full_runtime", fromlist=["price_rule"]).price_rule(
            conditions=[{"field": "csg_test", "op": "truth"}]))
    owned.clock["now"] = datetime.combine(cn_today(), time(14, 30, 15), CN_TZ).timestamp()
    prepare = owned.worker.prepare
    def inject(*args):
        account, context, payload, observed = prepare(*args)
        payload["strategy_task_ids"] = ["custom_test"]
        return account, context, payload, observed
    monkeypatch.setattr(owned.worker, "prepare", inject)
    def custom(payload, *_):
        daily = {**payload["daily_identities"]["custom_test"],
            "as_of": str(cn_today()), "rows": [], "total": 0,
            "sections": {"buy": [], "sell": [], "select": []},
            "monitor_contract": "strategy_monitor_v2",
            "monitor_tasks": {"buy": {"state": "rejected", "terminal": True, "passed": False}},
            "section_statuses": {"buy": "rejected"}}
        return {"results": {"custom_test": daily}, "state": {}, "events": [], "execution_events": []}
    monkeypatch.setattr(monitor_compute, "evaluate_strategy_phases", custom)
    def failed(*_args, **_kwargs):
        raise RuntimeError("ordinary sandbox unavailable")
    owned.worker.executor = SimpleNamespace(execute=failed)
    owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    saved = task_receipt(owned.documents, "custom_test", cn_today())
    assert saved["tasks"]["buy"]["state"] == "rejected"
    assert "ordinary sandbox unavailable" in owned.documents.get(
        "monitor_runtime", "default")["ordinary_compute_error"]


@pytest.mark.parametrize("frozen_quote", [False, True])
def test_real_worker_fifteen_seconds_confirmation_later_ticks_and_restart(owned, scan, monkeypatch, tmp_path, frozen_quote):
    from copy import deepcopy
    from dataclasses import replace
    from datetime import datetime, time

    import polars as pl
    from test_minute_signal_sections import CODE

    from app.market_time import CN_TZ, cn_today
    from app.multitenancy import monitor_compute, runtime
    from app.multitenancy.strategies import definition_payload
    from app.services.daily_selections import DailySelections
    from app.services.screener import ScreenerService
    from app.services.strategy_monitor_tasks import set_monitoring, task_receipt
    from app.services.strategy_results import capture_identity
    from app.strategy import custom_signals, overnight_runtime
    from app.strategy.basic_filter import previous_session

    engine, ctx, _ = scan
    day = cn_today()
    start = datetime.combine(day, time(14, 30), CN_TZ)
    context = replace(ctx, as_of=day,
        current=ctx.current.with_columns(pl.lit(day).alias("date")),
        daily_history=ctx.daily_history.with_columns(pl.lit(previous_session(day)).alias("date")),
        history=ctx.history.with_columns(pl.col("datetime").dt.replace(
            year=day.year, month=day.month, day=day.day)),
        observed_at=start, completed_through=start, realtime=True)
    owned.documents.put("strategy_sources", "custom_test", {
        "id": "custom_test", "source": "custom", "code": CODE,
        "definition": definition_payload(engine.get("custom_test"))})
    custom_signals.save_one(owned.documents, {
        "id": "trigger", "name": "尾盘", "kind": "entry", "timeframe": "intraday",
        "template": "late_session_strength", "enabled": True, "conditions": [],
        "params": {"time": "14:30:00", "end_time": "14:55:00",
                   "evaluation_delay_minutes": .5, "pool_up_min_pct": 60}})
    set_monitoring(owned.documents, "custom_test", "Test", True)
    identity = capture_identity(engine, owned.documents, "custom_test", timeframe="1m")
    DailySelections(owned.documents).save(identity, {
        "as_of": str(day), "source_date": str(previous_session(day)),
        "rows": context.current.filter(pl.col("symbol").str.starts_with("600")).to_dicts()}, started_at=1)
    if frozen_quote:
        from app.services.strategy_results import StrategyResults
        StrategyResults(owned.documents).save(identity, {
            "as_of": str(day), "section_mode": "signals", "rows": [], "total": 0,
            "sections": {"select": [], "buy": [], "sell": []},
            "section_statuses": {"buy": "unconfigured", "sell": "unconfigured"},
            "monitor_contract": "strategy_monitor_v2",
            "monitor_tasks": {kind: {"state": "unconfigured", "terminal": False}
                              for kind in ("buy", "sell")},
        }, started_at=int(start.timestamp() * 1_000_000_000))
    calls, reads = [], []
    owned.repo.get_minute_batch = lambda *_a, **_k: reads.append(True) or context.history
    monkeypatch.setattr(ScreenerService, "_load_enriched_history", lambda *_: context.daily_history)
    monkeypatch.setattr(overnight_runtime, "monitor_context", lambda *_a, **_k: replace(
        context, observed_at=datetime.fromtimestamp(owned.clock["now"], CN_TZ),
        completed_through=datetime.fromtimestamp(owned.clock["now"], CN_TZ)))
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: datetime.fromtimestamp(owned.clock["now"], CN_TZ))
    monkeypatch.setattr(runtime, "cn_now", lambda: datetime.fromtimestamp(owned.clock["now"], CN_TZ))
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime.fromtimestamp(owned.clock["now"], CN_TZ))
    prepare = owned.worker.prepare
    def preparing(*args):
        if frozen_quote:
            args[1]["sequence"] = int((start.timestamp() - 60) * 1000)
        account, access, payload, observed = prepare(*args)
        symbols = context.current["symbol"].to_list()
        payload["daily_pool_snapshots"] = {"custom_test": {
            "strategy_id": "custom_test", "as_of": str(day), "symbols": symbols, "total": len(symbols)}}
        return account, access, payload, observed
    monkeypatch.setattr(owned.worker, "prepare", preparing)
    def execute(payload, **_kwargs):
        calls.append(payload)
        # The real sandbox receives explicit inputs, never tenant documents.
        with custom_signals.private_signal_registry(payload["signals"]):
            return monitor_compute.evaluate_snapshot(payload, owned.repo, engine, tmp_path / "sandbox")
    owned.worker.executor = SimpleNamespace(execute=execute)
    tenant = owned.documents.context.tenant_id
    for second in (14, 15):
        owned.clock["now"] = start.timestamp() + second
        owned.worker.evaluate(tenant, owned.snapshot(status={"is_trading_hours": False}))
    assert calls == []
    first = task_receipt(owned.documents, "custom_test", day)
    assert first["tasks"]["buy"]["signals"]["trigger"]["breadth"]["state"] == "success"
    owned.clock["now"] = start.timestamp() + 30
    owned.worker.evaluate(tenant, owned.snapshot(status={"is_trading_hours": False}))
    done = deepcopy(task_receipt(owned.documents, "custom_test", day)["tasks"]["buy"])
    assert not done.get("errors"), done.get("errors")
    assert done["terminal"] is True, done
    assert done["state"] == "success"
    assert len(calls) == 1
    saved = owned.documents.get("monitor_results", "default")["results"]["custom_test"]
    assert saved["sections"]["buy"]
    for second in (31, 60, 120, 1501):
        owned.clock["now"] = start.timestamp() + second
        owned.worker.evaluate(tenant, owned.snapshot(status={"is_trading_hours": False}))
    assert len(calls) == 1
    assert task_receipt(owned.documents, "custom_test", day)["tasks"]["buy"] == done
    restarted = runtime.FullWorker(owned.env["db"], owned.repo, None, SimpleNamespace(execute=execute))
    try:
        restarted.evaluate(tenant, owned.snapshot(status={"is_trading_hours": False}))
        assert len(calls) == 1
    finally:
        restarted.close()
