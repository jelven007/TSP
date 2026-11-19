"""Monitoring health and the real-time new-buy deadline remain independent."""
# ruff: noqa: F401,F811 -- reuse isolated PostgreSQL fixtures.
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_critical_strategy_schedule import configure
from test_full_identity import env, full_cluster
from test_full_monitor import app_env
from test_full_runtime import owned, price_rule

from app.cell.context import AccessError
from app.market_time import CN_TZ, cn_today
from app.multitenancy import critical_schedule, health, monitor_rules, runtime
from app.multitenancy.alerts import Alerts
from app.multitenancy.monitor_rules import Rules
from app.services.strategy_results import StrategyResults, capture_identity
from app.strategy.engine import StrategyEngine


def test_retired_schedule_cannot_bypass_ordinary_monitor_quota(owned, monkeypatch):
    monkeypatch.setattr(monitor_rules, "MAX_RULES", 1)
    rules = Rules(owned.documents)
    rules.save_one(price_rule())
    configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    assert rules.load_one("critical_test") is None
    assert rules.load_one("my_price")["enabled"]
    with pytest.raises(AccessError, match="monitor_rule_limit"):
        rules.save_one(price_rule(id="another"))


@pytest.mark.parametrize("function", ["monitoring", "scheduled", "paper_trading"])
def test_compute_failure_is_not_reported_as_healthy_strategy_function(function):
    checks = {name: {"status": "ok"} for dependencies in health.FUNCTION_DEPENDENCIES.values()
              for name in dependencies}
    checks["compute_broker"] = {"status": "error"}
    result = health.function_health(checks)
    assert result[function]["ready"] is False
    assert "compute_broker" in result[function]["unavailable_dependencies"]
    assert result["market"]["ready"]


@pytest.mark.parametrize("commit_second,expected", [(0, "success"), (1, "expired")])
@pytest.mark.parametrize("prior_success,omit_deadline", [(False, False), (False, True), (True, True)])
def test_cross_deadline_result_cannot_archive_or_notify_new_buys(
    owned, monkeypatch, commit_second, expected, prior_success, omit_deadline,
):
    sid = "ma_golden_cross"
    day = cn_today()
    owned.clock["now"] = datetime.combine(day, time(14, 54, 59), CN_TZ).timestamp()
    owned.frame = owned.frame.with_columns(
        __import__("polars").lit(owned.clock["now"] * 1000).alias("quote_ts"))
    Rules(owned.documents).save_one(price_rule(conditions=[{"field": "csg_test", "op": "truth"}]))
    builtins = StrategyEngine([Path(runtime.__file__).resolve().parents[1] / "strategy/builtin"])
    identity = capture_identity(builtins, owned.documents, sid, timeframe="1m")
    prepare = owned.worker.prepare
    deadline = f"{day}T14:55:00"
    def inject(*args):
        account, context, payload, observed = prepare(*args)
        payload["daily_identities"][sid] = asdict(identity)
        payload["entry_deadlines"][sid] = deadline
        return account, context, payload, observed
    monkeypatch.setattr(owned.worker, "prepare", inject)
    row = {"symbol": "600519.SH", "close": 11.0, "score": 1}
    result = {"as_of": str(day), "strategy_id": sid, "runtime_identity": identity.runtime_identity,
              "rows": [row], "total": 1, "computed_at": int(owned.clock["now"] * 1000),
              "entry_deadline_at": deadline, "section_mode": "signals",
              "sections": {"select": [row], "buy": [row], "sell": []},
              "section_statuses": {"select": "success", "buy": "success", "sell": "success"}}
    previous = deepcopy(result)
    previous["sections"]["buy"] = [{"symbol": "600000.SH", "close": 9.0}]
    previous["computed_at"] -= 60_000
    if prior_success:
        StrategyResults(owned.documents).save(identity, previous, started_at=1)
    if omit_deadline:
        result.pop("entry_deadline_at")
    def execute(*_args, **_kwargs):
        owned.clock["now"] = datetime.combine(day, time(14, 55, commit_second), CN_TZ).timestamp()
        base = {"source": "strategy", "strategy_id": sid, "rule_id": "my_price",
                "ts": int(owned.clock["now"] * 1000), "symbol": "600519.SH", "message": "signal"}
        return {"state": {}, "results": {sid: deepcopy(result)},
                "events": [{**base, "type": kind} for kind in ("buy_signal", "sell_signal")],
                "execution_events": []}
    owned.worker.executor = SimpleNamespace(execute=execute)
    snapshot = owned.snapshot(frames={"stock": __import__(
        "app.multitenancy.market", fromlist=["encode_frame"]).encode_frame(owned.frame)})
    owned.worker.evaluate(owned.documents.context.tenant_id, snapshot)
    live = owned.documents.get("monitor_results", "default")["results"][sid]
    assert live["section_statuses"]["buy"] == expected
    events = Alerts(owned.documents).list_recent()
    assert any(event["type"] == "sell_signal" for event in events)
    archived = StrategyResults(owned.documents).read(identity, str(day))
    if expected == "expired":
        assert live["sections"]["buy"] == []
        assert not any(event["type"] == "buy_signal" for event in events)
        if prior_success:
            assert archived["result"]["sections"]["buy"] == previous["sections"]["buy"]
        else:
            assert archived is None
    else:
        assert live["sections"]["buy"] == [row]
        assert any(event["type"] == "buy_signal" for event in events)
        assert archived["result"]["sections"]["buy"] == [row]


def test_native_api_cannot_request_system_quota_bypass(app_env, monkeypatch):
    monkeypatch.setattr(monitor_rules, "MAX_RULES", 1)
    Rules(app_env["own"]).save_one(price_rule())
    response = app_env["client"].post("/api/monitor-rules", json=price_rule(
        id="forged", _system_task="late_session_critical_v1"))
    assert response.status_code == 429
    assert Rules(app_env["own"]).load_one("forged") is None


def test_late_entry_guard_preserves_sells_without_result_metadata():
    from app.strategy.execution_status import expire_late_entries
    result = {"results": {}, "events": [
        {"strategy_id": "s02", "type": kind} for kind in ("buy_signal", "pool_entry", "sell_signal")
    ]}
    result["execution_events"] = deepcopy(result["events"])
    expire_late_entries(result, {"s02": "2026-11-12T14:55:00"},
                        datetime(2026, 11, 12, 14, 55, 1, tzinfo=CN_TZ))
    assert [e["type"] for e in result["events"]] == ["sell_signal"]
    assert result["execution_events"] == result["events"]
