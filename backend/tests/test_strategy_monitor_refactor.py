"""The replacement monitor obeys explicit activation and immutable outcomes."""
from dataclasses import asdict
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl

from app.api import monitor_rules as api
from app.market_time import CN_TZ


def test_builtin_strategy_has_the_same_explicit_monitor_switch(tmp_path, monkeypatch):
    from app.api import strategy as strategy_api
    from app.strategy import monitor_rules

    strategy = SimpleNamespace(
        source="builtin", meta={"id": "ma_golden_cross", "name": "均线", "timeframes": ["1d"]},
        execution_backend="polars_expr",
    )
    monkeypatch.setattr(api, "_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(
        strategy_api, "_get_engine", lambda _: SimpleNamespace(get=lambda _: strategy)
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None)))
    enabled = api.set_strategy_monitoring(
        "ma_golden_cross", api.StrategyMonitoringModel(enabled=True), request
    )
    assert enabled["enabled"] is True
    assert any(rule["enabled"] for rule in monitor_rules.load_all(tmp_path))
    cancelled = api.set_strategy_monitoring(
        "ma_golden_cross", api.StrategyMonitoringModel(enabled=False), request
    )
    assert cancelled["enabled"] is False
    assert not any(rule["enabled"] for rule in monitor_rules.load_all(tmp_path))

def test_enabling_custom_strategy_requests_immediate_nightly_check(tmp_path, monkeypatch):
    from app.api import strategy as strategy_api
    calls = []
    strategy = SimpleNamespace(source="custom", meta={"name": "Alpha"})
    monkeypatch.setattr(api, "_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(strategy_api, "_get_engine",
                        lambda _: SimpleNamespace(get=lambda _: strategy))
    scheduler = SimpleNamespace(get_job=lambda _: True,
        modify_job=lambda job, **kwargs: calls.append((job, kwargs)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=None, scheduler=scheduler)))
    api.set_strategy_monitoring("custom_alpha", api.StrategyMonitoringModel(enabled=True), request)
    assert len(calls) == 1
    assert calls[0][0] == "daily_selections"
    assert calls[0][1]["next_run_time"].tzinfo == CN_TZ
    api.set_strategy_monitoring("custom_alpha", api.StrategyMonitoringModel(enabled=False), request)
    assert len(calls) == 1


def test_completed_breadth_rejection_keeps_its_original_receipt_across_ticks():
    from app.strategy.late_session_strength import evaluate

    day = date(2026, 11, 19)
    signal = {
        "id": "late", "enabled": True,
        "params": {
            "time": "14:30:00", "end_time": "14:55:00", "evaluation_delay_minutes": .5,
            "pool_up_min_pct": 60, "change_min_pct": .8, "change_max_pct": 4,
            "ma5_1m_count": 7, "ma5_5m_count": 4,
        },
    }
    minutes = pl.DataFrame({
        "symbol": ["600001.SH"], "datetime": [datetime(2026, 11, 19, 14, 30)],
        "close": [9.9],
    })
    state = {}
    first = evaluate(
        minutes, signal, day=day, pool_symbols=["600001.SH"], prev_close={"600001.SH": 10},
        completed_through=datetime(2026, 11, 19, 14, 30, 15), task_state=state,
    )
    assert first.stage == "breadth_rejected"
    repeated = evaluate(
        minutes, signal, day=day, pool_symbols=["600001.SH"], prev_close={"600001.SH": 10},
        completed_through=datetime(2026, 11, 19, 14, 31), task_state=state,
    )
    assert asdict(repeated) == asdict(first)


def test_task_summary_never_changes_a_completed_rejection_back_to_running(tmp_path):
    from test_minute_signal_sections import CODE

    from app.services.strategy_monitor_tasks import monitoring_summary, set_monitoring, task_receipt
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyEngine

    (tmp_path / "custom.py").write_text(CODE)
    engine = StrategyEngine([tmp_path])
    engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, {
        "id": "trigger", "name": "尾盘", "enabled": True, "kind": "entry",
        "timeframe": "intraday", "template": "late_session_strength", "conditions": [],
        "params": {
            "time": "14:30:00", "end_time": "14:55:00", "evaluation_delay_minutes": .5,
            "pool_up_min_pct": 60, "change_min_pct": .8, "change_max_pct": 4,
            "ma5_1m_count": 7, "ma5_5m_count": 4,
        },
    })
    set_monitoring(tmp_path, "custom_test", "Test", True)
    task_receipt(tmp_path, "custom_test", "2026-11-19", {
        "tasks": {"buy": {
            "state": "rejected", "status": "succeeded", "passed": False,
            "terminal": True, "reason": "上涨占比低于阈值",
        }},
    })
    now = datetime(2026, 11, 19, 14, 40, tzinfo=CN_TZ)
    result = monitoring_summary(engine, tmp_path, now=now)["strategies"][0]["tasks"]["buy"]
    assert result["state"] == "rejected"
    assert result["passed"] is False
    assert result["terminal"] is True

def test_nightly_summary_requires_normal_archive_instead_of_terminal_receipt(tmp_path):
    from test_minute_signal_sections import CODE

    from app.services.strategy_monitor_tasks import monitoring_summary, set_monitoring, task_receipt
    from app.strategy.engine import StrategyEngine
    (tmp_path/"custom.py").write_text(CODE)
    engine=StrategyEngine([tmp_path])
    engine._user_data_dir=lambda:tmp_path
    set_monitoring(tmp_path,"custom_test","Test",True)
    task_receipt(tmp_path,"custom_test","2026-11-19",{"tasks":{
        "select":{"state":"success","terminal":True}}})
    rows=monitoring_summary(engine,tmp_path,
        now=datetime(2026,11,19,20,59,tzinfo=CN_TZ))["strategies"]
    selected=rows[0]["tasks"]["select"]
    assert selected["state"]=="running"
    assert selected["terminal"] is False


def test_failure_trigger_record_includes_counts_and_all_stock_diagnostics():
    from app.multitenancy.monitor_compute import failure_events

    daily = {
        "as_of": "2026-11-19", "runtime_identity": "definition-v2",
        "monitor_tasks": {"buy": {
            "state": "failed", "errors": ["600001.SH 缺分钟", "600002.SH 昨收冲突"],
            "attempts": 2, "pool_total": 3, "pool_usable": 1, "missing_count": 2,
            "retry_until": "2026-11-19T14:30:30+08:00",
        }},
    }
    events, _ = failure_events(
        {"custom_test": daily}, [{"id": "mr", "strategy_id": "custom_test", "name": "Test"}],
        {}, datetime(2026, 11, 19, 14, 30, 20, tzinfo=CN_TZ),
    )
    assert len(events) == 1
    assert "600001.SH" in events[0]["message"]
    assert "600002.SH" in events[0]["message"]
    assert "完整池 3" in events[0]["message"]
    assert "有效 1" in events[0]["message"]
    assert "14:30:30" in events[0]["message"]


def test_phase_plan_uses_receipts_and_strict_breadth_deadline():
    from app.services.strategy_monitor_state import plan

    windows = {"buy": [{
        "start": "14:30:00", "end": "14:55:00",
        "signal": {"id": "late", "template": "late_session_strength", "params": {
            "evaluation_delay_minutes": .5, "pool_up_min_pct": 60}},
    }], "sell": []}
    def at(minute, second):
        return datetime(2026, 11, 19, 14, minute, second, tzinfo=CN_TZ)
    assert plan(windows, {}, at(30, 14), trading=True)["actions"] == []
    assert plan(windows, {}, at(30, 15), trading=True)["actions"][0]["phase"] == "breadth"
    assert plan(windows, {}, at(30, 30), trading=True)["actions"][0]["phase"] == "expire"
    passed = {"buy": {"signals": {"late": {"breadth": {"state": "success", "terminal": True}}}}}
    assert plan(windows, passed, at(30, 30), trading=True)["actions"][0]["phase"] == "stocks"
    done = {"buy": {"state": "rejected", "terminal": True}}
    assert plan(windows, done, at(31, 0), trading=True)["actions"] == []
    assert plan(windows, {}, at(30, 15), trading=False)["actions"] == []


def test_cancelled_generation_rejects_late_result(tmp_path):
    from app.services.strategy_monitor_tasks import assert_generation, set_monitoring

    first = set_monitoring(tmp_path, "custom_test", "Test", True)
    set_monitoring(tmp_path, "custom_test", "Test", False)
    import pytest
    with pytest.raises(ValueError, match="监控"):
        assert_generation(tmp_path, "custom_test", first["generation"])


def test_breadth_stage_uses_shared_kernel_without_starting_strategy_sandbox(monkeypatch):
    from app.multitenancy import monitor_compute
    from app.services.screener import ScreenerService
    from app.services.strategy_monitor_state import plan

    now = datetime(2026, 11, 19, 14, 30, 15, tzinfo=CN_TZ)
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: now)
    signal = {"id": "late", "template": "late_session_strength", "enabled": True, "params": {
        "time": "14:30:00", "end_time": "14:55:00", "evaluation_delay_minutes": .5,
        "pool_up_min_pct": 60}}
    windows = {"buy": [{"start": "14:30:00", "end": "14:55:00", "signal": signal}], "sell": []}
    daily = pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 11, 18)], "close": [10.]})
    minutes = pl.DataFrame({"symbol": ["600001.SH"],
                            "datetime": [datetime(2026, 11, 19, 14, 30)], "close": [10.1]})
    monkeypatch.setattr(ScreenerService, "_load_enriched_history", lambda *_: daily)
    repo = SimpleNamespace(get_minute_batch=lambda *_a, **_k: minutes)
    def forbidden(*_a, **_k):
        raise AssertionError("广度阶段不应启动用户沙箱")
    payload = {
        "state": {}, "strategy_task_ids": ["custom_test"],
        "strategy_task_plans": {"custom_test": plan(windows, {}, now, trading=True)},
        "strategy_task_windows": {"custom_test": windows},
        "late_task_scopes": {"custom_test": "custom_test:def"},
        "daily_pool_snapshots": {"custom_test": {"symbols": ["600001.SH"]}},
        "strategy_task_receipts": {}, "strategy_task_results": {},
        "daily_identities": {"custom_test": {"runtime_identity": "def", "asset_type": "stock", "timeframe": "1m"}},
        "selection_snapshots": {}, "sell_rosters": {},
    }
    computed = monitor_compute.evaluate_strategy_phases(payload, repo, SimpleNamespace(execute=forbidden))
    task = computed["results"]["custom_test"]["monitor_tasks"]["buy"]
    assert task["signals"]["late"]["breadth"]["state"] == "success"
    assert task["terminal"] is False
    assert task["pool_up_pct"] == 100.


def test_completed_controller_does_not_call_strategy_again():
    from app.services.strategy_monitor_tasks import run_tasks
    from app.strategy.engine import StrategyDataContext

    done = {"state": "rejected", "terminal": True, "passed": False, "completed_at": "14:30:15"}
    engine = SimpleNamespace(_monitor_receipt_loader=lambda _: {"tasks": {"buy": done}},
                             _selection_loader=lambda *_: {"rows": []})
    ctx = StrategyDataContext("stock", "1m", date(2026, 11, 19),
                              observed_at=datetime(2026, 11, 19, 14, 40, tzinfo=CN_TZ))
    result = run_tasks(engine, SimpleNamespace(meta={"id": "custom_test"}), ctx, {},
                       {"buy": [{"start": "14:30:00", "end": "14:55:00"}], "sell": []}, None)
    assert result.monitor_tasks["buy"]["state"] == "rejected"
    assert result.monitor_tasks["buy"]["completed_at"] == "14:30:15"


def test_success_and_rejected_phases_also_create_detailed_trigger_records():
    from app.multitenancy.monitor_compute import failure_events

    daily = {"as_of": "2026-11-19", "monitor_tasks": {"buy": {"signals": {"late": {
        "breadth": {"state": "rejected", "terminal": True, "passed": False,
                    "attempts": 1, "pool_total": 100, "pool_usable": 98, "missing_count": 2},
    }}}}}
    rules = [{"id": "rule", "strategy_id": "custom_test"}]
    now = datetime(2026, 11, 19, 14, 30, 15, tzinfo=CN_TZ)
    events, state = failure_events({"custom_test": daily}, rules, {}, now)
    assert len(events) == 1
    assert events[0]["type"] == "signal_calculation_succeeded"
    assert "条件未满足" in events[0]["message"]
    assert events[0]["details"]["pool_total"] == 100
    assert failure_events({"custom_test": daily}, rules, state, now)[0] == []


def test_independent_signals_publish_and_accumulate_without_rerunning_completed(monkeypatch):
    from app.services.strategy_monitor_tasks import run_tasks
    from app.strategy import version
    from app.strategy.engine import StrategyDataContext, StrategyResult

    monkeypatch.setattr(version, "runtime_identity", lambda *_: "definition")
    saved, prior, calls = {}, {}, []
    def execute(sid, context, *, overrides, **kwargs):
        signal = overrides["entry_signals"][0]
        calls.append(signal)
        symbol = "600001.SH" if signal == "csgi_first" else "600002.SH"
        return StrategyResult(context.as_of, sid, [{"symbol": symbol, "score": 1.}], 1,
                              entry_signal_hits=[{"symbol": symbol, "signals": [signal]}])
    engine = SimpleNamespace(_monitor_receipt_loader=lambda _: saved,
                             _monitor_result_loader=lambda _: prior,
                             _selection_loader=lambda *_: {"rows": []}, run=execute)
    windows = {"buy": [{"start": start, "end": "14:55:00",
                        "signal": {"id": sid, "params": {}}}
                       for sid, start in (("first", "14:30:00"), ("second", "14:31:00"))],
               "sell": []}
    ctx = StrategyDataContext("stock", "1m", date(2026, 11, 19),
                              observed_at=datetime(2026, 11, 19, 14, 30, tzinfo=CN_TZ))
    first = run_tasks(engine, SimpleNamespace(meta={"id": "custom_test"}), ctx, {}, windows, None)
    assert first.sections["buy"] == first.rows
    assert len(first.rows) == 1
    assert first.monitor_tasks["buy"]["terminal"] is False
    saved.update(tasks=first.monitor_tasks)
    prior.update(asdict(first))
    ctx.observed_at = datetime(2026, 11, 19, 14, 31, tzinfo=CN_TZ)
    second = run_tasks(engine, SimpleNamespace(meta={"id": "custom_test"}), ctx, {}, windows, None)
    assert calls == ["csgi_first", "csgi_second"]
    assert {r["symbol"] for r in second.sections["buy"]} == {"600001.SH", "600002.SH"}
    assert second.monitor_tasks["buy"]["terminal"] is True


def test_waiting_receipt_cannot_be_shown_as_previous_success():
    from app.strategy.execution_status import merge_result
    previous = {"as_of": "2026-11-19", "runtime_identity": "v2", "computed_at": 1,
                "execution_status": {"state": "waiting"}, "rows": [], "total": 0}
    result = merge_result(previous, {**previous, "execution_status": {"state": "failed"}})
    assert not result.get("showing_last_success")


def test_sell_signals_keep_independent_errors_and_first_hits_through_expiry(monkeypatch):
    from app.services import strategy_monitor_tasks as module
    saved, prior, calls = {}, {}, []
    windows = {"buy": [], "sell": [
        {"start": "09:30:00", "end": "11:30:00", "signal": {"id": sig}}
        for sig in ("first", "broken")]}
    def evaluate(_engine, _strategy, _context, _overrides, active, roster):
        sig = active["sell"][0]["signal"]["id"]
        calls.append(sig)
        return {"hits": [{"symbol": "600001.SH", "trigger_time": "09:30:00"}] if sig == "first" else [],
                "errors": [] if sig == "first" else ["600002.SH 缺分钟"]}
    monkeypatch.setattr(module, "evaluate_sell_task", evaluate)
    engine = SimpleNamespace(_monitor_receipt_loader=lambda _: saved,
        _monitor_result_loader=lambda _: prior, _selection_loader=lambda *_: {"rows": []})
    ctx = module.sell_context(SimpleNamespace(repo=SimpleNamespace(
        get_minute_batch=lambda *_a, **_k: pl.DataFrame()),
        _load_enriched_history=lambda *_: pl.DataFrame()), date(2026, 11, 19),
        pl.DataFrame(), None, datetime(2026, 11, 19, 9, 30, tzinfo=CN_TZ))
    roster = {"rows": [{"symbol": "600001.SH"}, {"symbol": "600002.SH"}]}
    result = module.run_tasks(engine, SimpleNamespace(meta={"id": "custom_test"}),
                              ctx, {}, windows, roster)
    assert calls == ["first", "broken"]
    signals = result.monitor_tasks["sell"]["signals"]
    assert signals["first"]["errors"] == []
    assert signals["broken"]["errors"] == ["600002.SH 缺分钟"]
    saved.update(tasks=result.monitor_tasks)
    prior.update(asdict(result))
    ctx.observed_at = datetime(2026, 11, 19, 11, 30, 1, tzinfo=CN_TZ)
    expired = module.run_tasks(engine, SimpleNamespace(meta={"id": "custom_test"}),
                               ctx, {}, windows, roster)
    assert expired.exit_signal_hits == result.exit_signal_hits
    assert expired.monitor_tasks["sell"]["signals"]["broken"]["first_error"] == "600002.SH 缺分钟"


def test_expiring_second_buy_keeps_completed_first_and_does_not_reject_saved_rows(monkeypatch):
    from app.services.strategy_monitor_tasks import fence_tasks, run_tasks
    from app.strategy.engine import StrategyDataContext
    row = {"symbol": "600001.SH", "score": 1.}
    first = {"terminal": True, "state": "success", "stocks": {
        "terminal": True, "state": "success", "rows": [row]}}
    saved = {"tasks": {"buy": {"signals": {"first": first}}}}
    prior = {"as_of": "2026-11-19", "rows": [row], "total": 1, "sections": {
        "buy": [row], "sell": [], "select": []}, "section_statuses": {}}
    engine = SimpleNamespace(_monitor_receipt_loader=lambda _: saved,
        _monitor_result_loader=lambda _: prior, _selection_loader=lambda *_: {"rows": []})
    windows = {"buy": [{"start": "14:30:00", "end": "14:55:00", "signal": {"id": sig}}
                        for sig in ("first", "second")], "sell": []}
    ctx = StrategyDataContext("stock", "1m", date(2026, 11, 19),
        observed_at=datetime(2026, 11, 19, 14, 55, 1, tzinfo=CN_TZ))
    result = run_tasks(engine, SimpleNamespace(meta={"id": "custom_test"}), ctx, {}, windows, None)
    assert result.sections["buy"] == [row]
    assert result.monitor_tasks["buy"]["published"] is True
    assert fence_tasks(asdict(result), windows, ctx.observed_at) == set()


def test_commit_guard_discards_only_new_late_signal_and_keeps_prior_buy():
    from app.services.strategy_monitor_state import CONTRACT
    from app.services.strategy_monitor_tasks import fence_tasks
    old, new = {"symbol": "OLD"}, {"symbol": "NEW"}
    def signal(row):
        return {"state": "success", "stocks": {"state": "success", "rows": [row]}}
    daily = {"monitor_contract": CONTRACT, "sections": {"buy": [old, new]},
        "rows": [old, new], "monitor_tasks": {"buy": {"state": "running",
            "executed_signals": ["second"], "signals": {"first": signal(old), "second": signal(new)}}}}
    windows = {"buy": [{"start": "14:30:00", "end": "14:55:00", "signal": {"id": sig}}
                       for sig in ("first", "second")]}
    rejected = fence_tasks(daily, windows, datetime(2026, 11, 19, 14, 55, 1, tzinfo=CN_TZ))
    assert rejected == {"buy"}
    assert daily["sections"]["buy"] == [old]
    assert daily["monitor_tasks"]["buy"]["published"] is True


def test_precompute_failure_preserves_partial_completed_buy_and_missing_signal():
    from app.multitenancy.monitor_compute import failed_phase_results
    from app.services.strategy_monitor_state import CONTRACT
    row = {"symbol": "600001.SH"}
    done = {"state": "success", "terminal": True,
            "stocks": {"state": "success", "terminal": True, "rows": [row]}}
    payload = {"strategy_task_ids": ["custom_test"], "daily_identities": {"custom_test": {}},
        "strategy_task_results": {"custom_test": {"as_of": "2026-11-19", "rows": [row],
            "sections": {"buy": [row], "sell": [], "select": []}}},
        "strategy_task_receipts": {"custom_test": {"tasks": {"buy": {"signals": {"first": done}}}}},
        "strategy_task_windows": {"custom_test": {"buy": [{"signal": {"id": sig}}
                                                       for sig in ("first", "second", "third")]}},
        "strategy_task_plans": {"custom_test": {"actions": [{
            "kind": "buy", "signal_id": "second", "phase": "expire", "expired_phase": "stocks",
            "scheduled_at": "2026-11-19T14:30:30+08:00",
            "retry_until": "2026-11-19T14:55:00+08:00"}]}}}
    failed = failed_phase_results(payload, "计算进程失败",
                                  datetime(2026, 11, 19, 14, 55, 1, tzinfo=CN_TZ))["custom_test"]
    assert failed["monitor_contract"] == CONTRACT
    task = failed["monitor_tasks"]["buy"]
    assert task["signals"]["first"] == done
    assert task["signals"]["second"]["stocks"]["state"] == "expired"
    assert task["terminal"] is False
    assert task["published"] is True
    assert failed["sections"]["buy"] == [row]


def test_single_strategy_api_keeps_canonical_task_receipt(monkeypatch):
    from app.api import screener
    daily = {"as_of": "2026-11-19", "rows": [], "total": 0,
        "monitor_contract": "strategy_monitor_v2",
        "monitor_tasks": {"buy": {"state": "rejected", "terminal": True}},
        "monitor_executed_signals": {"buy": []}}
    monkeypatch.setattr(screener, "_history_payload", lambda *_a, **_k: {
        "results": {"custom_test": daily}})
    monkeypatch.setattr(screener, "_history_identities", lambda *_a, **_k: (
        SimpleNamespace(memberships=lambda *_: {}), []))
    result = screener.get_cached_result("custom_test",
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None))),
        ext_columns=None, as_of=date(2026, 11, 19))["result"]
    assert result["monitor_contract"] == daily["monitor_contract"]
    assert result["monitor_tasks"] == daily["monitor_tasks"]


def test_repeated_failures_keep_bounded_receipt_and_full_current_alert():
    import json
    from datetime import timedelta

    from app.multitenancy.monitor_compute import failure_events
    from app.services.strategy_monitor_state import receipt
    now = datetime(2026, 11, 19, 20, tzinfo=CN_TZ)
    action = {"phase": "select", "scheduled_at": now.isoformat(),
              "retry_until": now.replace(hour=22).isoformat()}
    saved = {}
    for attempt in range(200):
        saved = receipt(saved, action, now + timedelta(seconds=attempt * 3),
                        errors=["数据读取失败"], details={"diagnostic_context": "x" * 10000})
    assert saved["attempts"] == 200
    assert saved["started_at"] == now.isoformat()
    assert saved["first_error"] == "数据读取失败"
    assert len(json.dumps(saved).encode()) < 500000
    events, _ = failure_events({"custom_test": {"as_of": str(now.date()),
        "monitor_tasks": {"select": saved}}}, [{"type": "strategy", "strategy_id": "custom_test",
        "id": "rule", "name": "测试"}], {}, now)
    assert len(events) == 1
    assert events[0]["details"]["diagnostic_context"] == "x" * 10000
    assert "history" not in events[0]["details"]
