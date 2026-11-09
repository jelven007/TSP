"""A closed/stale quote stream must not leave live minute entries waiting forever."""
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_runtime import owned as owned

from app.api import screener
from app.cell.admin import migrate
from app.cell.context import AccessError
from app.cell.scheduler import Scheduler
from app.market_time import CN_TZ, cn_today
from app.multitenancy.events import EventLog
from app.multitenancy.monitor_rules import Rules
from app.multitenancy.runtime import fingerprint
from app.multitenancy.strategies import TenantStrategyEngine, definition_payload
from app.services.strategy_results import StrategyResults, capture_identity
from app.strategy import custom_signals, monitor_rules
from app.strategy.engine import StrategyDef, StrategyEngine

SID = "custom_expiry"


@pytest.fixture(scope="module", autouse=True)
def upgrade(full_cluster):
    for kind, role in zip(("control", "data"), full_cluster["roles"], strict=True):
        migrate(full_cluster[kind], kind, role, "full", target_version=7)
    full_cluster["db"].settings = full_cluster["db"].settings.model_copy(update={"schema_version": 7})
    full_cluster["db"].check()


@pytest.fixture
def late(owned):
    sig = {"id": "late", "name": "尾盘走强", "kind": "entry", "timeframe": "intraday",
           "template": "late_session_strength", "conditions": [], "enabled": True,
           "params": {"time": "14:30:00", "evaluation_delay_minutes": 0.5}}
    custom_signals.save_one(owned.documents, sig)
    definition = StrategyDef(
        meta={"id": SID, "name": "过期测试", "asset_types": ["stock"], "timeframes": ["1m"],
              "result_sections": "signals"},
        basic_filter={}, entry_signals=["csgi_late"], exit_signals=[],
        execution_backend="minute_filter", source="custom", logic_version="fixture",
        stop_loss=None, trailing_stop=None, trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None, max_hold_days=None,
        filter_fn=None, filter_history_fn=None, lookback_days=30,
    )
    owned.documents.put("strategy_sources", SID, {
        "source": "custom", "code": "", "definition": definition_payload(definition)})
    Rules(owned.documents).save_one(monitor_rules.normalize({
        "id": "late", "name": "尾盘监控", "type": "strategy", "strategy_id": SID, "scope": "all"}))
    builtins = StrategyEngine([Path(__file__).resolve().parents[1] / "app/strategy/builtin"])
    engine = TenantStrategyEngine(owned.documents, builtins, SimpleNamespace())
    identity = capture_identity(engine, owned.documents, SID, timeframe="1m")
    owned.clock["now"] = datetime.combine(cn_today(), time(15, 2), CN_TZ).timestamp()
    return owned, identity


def saved_result(identity, *, buy="waiting", rows=()):
    return {"strategy": SID, "as_of": str(cn_today()), "section_mode": "signals",
            "runtime_identity": identity.runtime_identity, "computed_at": 123,
            "total": len(rows), "rows": list(rows),
            "sections": {"select": [{"symbol": "600000.SH"}], "buy": list(rows), "sell": []},
            "section_statuses": {"select": "success", "buy": buy, "sell": "success"},
            "section_errors": {"buy": "等待14:30:30确认入场触发器"} if buy != "success" else {}}


def request_for(monkeypatch, documents):
    monkeypatch.setattr(screener, "_private_data_dir", lambda _: documents)
    return SimpleNamespace()


@pytest.mark.parametrize("trading,buy", [(False, "waiting"), (True, "waiting"), (False, "failed")])
def test_unconfirmed_entry_expires_without_fresh_market_and_preserves_archive(late, monkeypatch, trading, buy):
    owned, identity = late
    store = StrategyResults(owned.documents)
    previous = store.save(identity, saved_result(identity, buy=buy), started_at=1)["result"]
    records = owned.documents.records("strategy_result_history")
    tenant = owned.documents.context.tenant_id
    assert owned.worker.evaluate(tenant, owned.snapshot(status={"is_trading_hours": trading},
                                captured_at=owned.clock["now"] - 60)) == 0
    attempt = owned.documents.get("strategy_execution_status", SID) or {}
    assert attempt.get("state") == "expired"
    assert attempt["runtime_identity"] == identity.runtime_identity
    assert attempt["entry_deadline_at"] == f"{cn_today()}T14:55:00"
    request = request_for(monkeypatch, owned.documents)
    view = screener._with_execution_status(request, {SID: previous}, str(cn_today()), identities=[identity])[SID]
    assert view["execution_status"]["state"] == "expired"
    assert view["section_statuses"] == {"select": "success", "buy": "expired", "sell": "success"}
    assert "14:55:00" in view["section_errors"]["buy"]
    assert "等待14:30:30" not in view["section_errors"]["buy"]
    assert view["sections"] == previous["sections"]
    assert owned.documents.records("strategy_result_history") == records
    assert owned.documents.get("monitor_results", "default") is None
    refreshes = [event for event in EventLog(owned.documents).since(0)
                 if event["kind"] == "strategy_results_updated"]
    assert len(refreshes) == 1
    # Restarts and subsequent waiting ticks retain the same terminal state.
    owned.clock["now"] += 30
    owned.worker.evaluate(tenant, owned.snapshot(status={"is_trading_hours": False}))
    assert owned.documents.get("strategy_execution_status", SID)["state"] == "expired"
    assert [event for event in EventLog(owned.documents).since(0)
            if event["kind"] == "strategy_results_updated"] == refreshes


@pytest.mark.parametrize("offset,expected", [(0, None), (0.001, "expired")])
def test_live_cutoff_is_inclusive_at_exact_second(late, offset, expected):
    owned, _ = late
    owned.clock["now"] = datetime.combine(cn_today(), time(14, 55), CN_TZ).timestamp() + offset
    owned.worker.evaluate(owned.documents.context.tenant_id,
                          owned.snapshot(status={"is_trading_hours": False}))
    assert (owned.documents.get("strategy_execution_status", SID) or {}).get("state") == expected


@pytest.mark.parametrize("rows", [[], [{"symbol": "600519.SH"}]])
def test_already_confirmed_buy_remains_success_after_close(late, monkeypatch, rows):
    owned, identity = late
    store = StrategyResults(owned.documents)
    previous = store.save(identity, saved_result(identity, buy="success", rows=rows), started_at=1)["result"]
    records = owned.documents.records("strategy_result_history")
    owned.worker.evaluate(owned.documents.context.tenant_id,
                          owned.snapshot(status={"is_trading_hours": False}))
    request = request_for(monkeypatch, owned.documents)
    view = screener._with_execution_status(request, {SID: previous}, str(cn_today()), identities=[identity])[SID]
    assert view["execution_status"]["state"] == "success"
    assert view["section_statuses"]["buy"] == "success"
    assert view["sections"] == previous["sections"]
    assert owned.documents.records("strategy_result_history") == records


@pytest.mark.parametrize("change", ["fingerprint", "identity", "day"])
def test_api_rejects_terminal_status_from_other_definition_or_session(late, monkeypatch, change):
    owned, identity = late
    previous = saved_result(identity)
    with owned.documents.transaction() as unit:
        attempt = {"state": "expired", "reason": "已截止", "updated_at": 123,
                   "as_of": str(cn_today()), "runtime_identity": identity.runtime_identity,
                   "fingerprint": fingerprint(unit.connection, include_state=False),
                   "entry_deadline_at": f"{cn_today()}T14:55:00"}
        attempt[{"fingerprint": "fingerprint", "identity": "runtime_identity", "day": "as_of"}[change]] = "other"
        unit.put("strategy_execution_status", SID, attempt)
    request = request_for(monkeypatch, owned.documents)
    assert screener._with_execution_status(request, {SID: previous}, str(cn_today()), identities=[identity])[SID] == previous
    yesterday = replace(identity, runtime_identity="historical")
    assert screener._with_execution_status(request, {SID: previous}, "2026-10-30", identities=[yesterday])[SID] == previous
    request_for(monkeypatch, owned.other)
    assert screener._with_execution_status(request, {SID: previous}, str(cn_today()), identities=[identity])[SID] == previous


def test_expired_scheduler_claim_cannot_publish_expiry(late):
    owned, _ = late
    scheduler = Scheduler(owned.env["db"])
    claim = next(row for row in scheduler.claim("realtime", 32)
                 if row["tenant_id"] == owned.documents.context.tenant_id)
    scheduler.finish(claim, None)
    with pytest.raises(AccessError, match="租约已过期"):
        owned.worker.evaluate(owned.documents.context.tenant_id,
                              owned.snapshot(status={"is_trading_hours": False}), claim)
    assert owned.documents.get("strategy_execution_status", SID) is None


@pytest.mark.parametrize("disabled", ["signal", "override"])
def test_disabled_entry_has_no_cutoff_transition(late, disabled):
    owned, _ = late
    if disabled == "override":
        owned.documents.put("strategy_overrides", SID, {"entry_signals": []})
    else:
        sig = owned.documents.get("custom_signals", "late")
        custom_signals.save_one(owned.documents, {**sig, "enabled": False})
    owned.worker.evaluate(owned.documents.context.tenant_id,
                          owned.snapshot(status={"is_trading_hours": False}))
    assert owned.documents.get("strategy_execution_status", SID) is None


def test_configuration_edit_before_commit_rejects_cutoff_transition(late, monkeypatch):
    owned, _ = late
    original = owned.worker.prepare

    def changed(*args):
        prepared = original(*args)
        owned.documents.put("strategy_overrides", SID, {"entry_signals": []})
        return prepared

    monkeypatch.setattr(owned.worker, "prepare", changed)
    with pytest.raises(AccessError, match="监控配置已更新"):
        owned.worker.evaluate(owned.documents.context.tenant_id,
                              owned.snapshot(status={"is_trading_hours": False}))
    assert owned.documents.get("strategy_execution_status", SID) is None


@pytest.mark.parametrize("removed", [True, False])
def test_unconfirmed_day_expires_when_monitor_removed_or_disabled(late, monkeypatch, removed):
    owned, identity = late
    previous = StrategyResults(owned.documents).save(identity, saved_result(identity), started_at=1)["result"]
    rules = Rules(owned.documents)
    if removed:
        rules.delete_one("late")
    else:
        rules.save_one({**rules.load_one("late"), "enabled": False})
    before = rules.load_all()
    owned.worker.evaluate(owned.documents.context.tenant_id,
                          owned.snapshot(status={"is_trading_hours": False}))
    request = request_for(monkeypatch, owned.documents)
    view = screener._with_execution_status(request, {SID: previous}, str(cn_today()), identities=[identity])[SID]
    assert view["execution_status"]["state"] == "expired"
    assert view["section_statuses"]["buy"] == "expired"
    assert rules.load_all() == before
