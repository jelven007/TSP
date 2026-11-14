"""Critical task completion must be real, bounded, and independently observable."""
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


def test_system_schedule_recovers_when_ordinary_monitor_quota_is_full(owned, monkeypatch):
    monkeypatch.setattr(monitor_rules, "MAX_RULES", 1)
    rules = Rules(owned.documents)
    rules.save_one(price_rule())
    configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    assert rules.load_one("critical_test")["enabled"]
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
    identity = capture_identity(builtins, owned.documents, sid)
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


def test_scheduled_watchdog_detects_missing_run_without_realtime_worker(owned):
    configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    now = datetime(2026, 11, 12, 14, 31, 30, tzinfo=CN_TZ)
    critical_schedule.check_completion(owned.worker, owned.documents, now=now)
    critical_schedule.check_completion(owned.worker, owned.documents, now=now)
    alerts = Alerts(owned.documents).list_recent()
    assert len(alerts) == 1
    assert alerts[0]["type"] == "critical_task_delayed"
    assert alerts[0]["strategy_id"] == "custom_test"
    assert "未完成" in alerts[0]["message"]
    assert owned.documents.get("critical_strategy_runs", "custom_test:2026-11-12")["state"] == "delayed"


@pytest.mark.parametrize("buy_count", [0, 1])
def test_watchdog_accepts_successful_zero_or_nonzero_buy_result(owned, buy_count):
    sid = configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    day = "2026-11-12"
    with owned.documents.transaction() as unit:
        unit.put("monitor_results", "default", {
            "fingerprint": runtime.fingerprint(unit.connection, include_state=False),
            "results": {sid: {"as_of": day, "section_statuses": {"buy": "success"},
                              "computed_at": int(datetime(2026, 11, 12, 14, 30, 45, tzinfo=CN_TZ).timestamp()*1000),
                              "sections": {"buy": [{"symbol": "600000.SH"}] * buy_count}}},
        })
    critical_schedule.check_completion(
        owned.worker, owned.documents, now=datetime(2026, 11, 12, 14, 31, 30, tzinfo=CN_TZ))
    receipt = owned.documents.get("critical_strategy_runs", sid + ":" + day)
    assert receipt["state"] == "succeeded" and receipt["buy_count"] == buy_count
    assert Alerts(owned.documents).count() == 0


def test_watchdog_rejects_stale_day_and_never_replays_after_deadline(owned):
    sid = configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    owned.documents.put("monitor_results", "default", {
        "results": {sid: {"as_of": "2026-11-11", "section_statuses": {"buy": "success"}}},
    })
    owned.worker.executor = SimpleNamespace(execute=lambda *_a, **_k: pytest.fail("late replay"))
    critical_schedule.check_completion(
        owned.worker, owned.documents, now=datetime(2026, 11, 12, 14, 55, 1, tzinfo=CN_TZ))
    receipt = owned.documents.get("critical_strategy_runs", sid + ":2026-11-12")
    assert receipt["state"] == "expired"
    assert Alerts(owned.documents).list_recent()[0]["type"] == "critical_task_expired"


@pytest.mark.parametrize("moment", [
    datetime(2026, 11, 12, 14, 30, 30, tzinfo=CN_TZ),
    datetime(2026, 11, 14, 14, 31, 30, tzinfo=CN_TZ),
])
def test_watchdog_does_not_alert_before_grace_or_on_closed_day(owned, moment):
    configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    critical_schedule.check_completion(owned.worker, owned.documents, now=moment)
    assert Alerts(owned.documents).count() == 0


@pytest.mark.parametrize("invalid", ["fingerprint", "selection_only", "showing_last_success",
                                    "failed", "before_confirmation", "late", "missing_timestamp"])
def test_watchdog_does_not_misreport_unconfirmed_results_as_success(owned, invalid):
    sid = configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    daily = {"as_of": "2026-11-12", "section_statuses": {"buy": "success"},
             "computed_at": int(datetime(2026, 11, 12, 14, 30, 45, tzinfo=CN_TZ).timestamp() * 1000),
             "sections": {"buy": []}}
    if invalid in ("selection_only", "showing_last_success"):
        daily[invalid] = True
    elif invalid == "failed":
        daily["execution_status"] = {"state": "failed"}
    elif invalid == "missing_timestamp":
        daily.pop("computed_at")
    elif invalid == "before_confirmation":
        daily["computed_at"] -= 60_000
    elif invalid == "late":
        daily["computed_at"] += 30 * 60_000
    with owned.documents.transaction() as unit:
        unit.put("monitor_results", "default", {
            "fingerprint": "stale" if invalid == "fingerprint" else runtime.fingerprint(
                unit.connection, include_state=False), "results": {sid: daily},
        })
    critical_schedule.check_completion(
        owned.worker, owned.documents, now=datetime(2026, 11, 12, 14, 56, tzinfo=CN_TZ))
    assert owned.documents.get("critical_strategy_runs", sid + ":2026-11-12")["state"] == "expired"


@pytest.mark.parametrize("unavailable", ["schedule", "source", "signal", "owner", "claim"])
def test_watchdog_respects_authorization_and_available_inputs(owned, unavailable):
    sid = configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    claim = None
    if unavailable == "schedule":
        owned.documents.put("critical_strategy_schedule", sid, {"enabled": False})
    elif unavailable == "source":
        owned.documents.delete("strategy_sources", sid)
    elif unavailable == "signal":
        owned.documents.delete("custom_signals", "late_session_strength")
    elif unavailable == "owner":
        with owned.worker.db.control.connection() as conn:
            conn.execute("UPDATE control.users SET active=false WHERE id=%s",
                         (owned.documents.context.user_id,))
    else:
        claim = {"tenant_id": owned.documents.context.tenant_id, "kind": "scheduled", "owner": "expired"}
    from contextlib import nullcontext
    with pytest.raises(AccessError) if unavailable in ("owner", "claim") else nullcontext():
        critical_schedule.check_completion(
            owned.worker, owned.documents, claim, now=datetime(2026, 11, 12, 14, 56, tzinfo=CN_TZ))
    assert owned.documents.records("critical_strategy_runs") == []
    assert Alerts(owned.documents).count() == 0


def test_scheduled_worker_checks_before_slow_jobs_and_without_compute(owned, monkeypatch):
    import asyncio

    from test_full_schedules import claim

    from app.multitenancy.scheduled_worker import TenantSchedules
    sid = configure(owned)
    service = TenantSchedules(owned.env["db"], None)
    service.executor = SimpleNamespace(execute=lambda *_a, **_k: pytest.fail("watchdog called compute"))
    calls = []
    def slow_job(*_args):
        assert owned.documents.get("critical_strategy_runs", sid + ":2026-11-12")["state"] == "delayed"
        calls.append(True)
        raise RuntimeError("later data job failed")
    monkeypatch.setattr(service, "daily_selections", slow_job)
    owned.env["data"].execute(
        """INSERT INTO scheduler.work(tenant_id,kind,due_at) VALUES(%s,'scheduled',now())
           ON CONFLICT(tenant_id,kind) DO UPDATE SET due_at=excluded.due_at""",
        (owned.documents.context.tenant_id,),
    )
    try:
        with pytest.raises(RuntimeError, match="later data job"):
            asyncio.run(service.process(claim(owned.env, owned.documents), {}, None,
                                        now=datetime(2026, 11, 12, 14, 31, 30, tzinfo=CN_TZ)))
        assert calls == [True]
        assert len(Alerts(owned.documents).list_recent()) == 1
        assert owned.other.records("critical_strategy_runs") == []
        with owned.documents.transaction(write=False) as unit:
            assert unit.connection.execute("SELECT count(*) AS n FROM business.notification_outbox").fetchone()["n"] == 0
    finally:
        service.runtime.close()


def test_system_rule_does_not_consume_ordinary_quota(owned, monkeypatch):
    monkeypatch.setattr(monitor_rules, "MAX_RULES", 1)
    configure(owned)
    critical_schedule.reconcile(owned.worker, owned.documents)
    Rules(owned.documents).save_one(price_rule())
    assert len(Rules(owned.documents).load_all()) == 2


def test_native_api_cannot_request_system_quota_bypass(app_env, monkeypatch):
    monkeypatch.setattr(monitor_rules, "MAX_RULES", 1)
    Rules(app_env["own"]).save_one(price_rule())
    response = app_env["client"].post("/api/monitor-rules", json=price_rule(
        id="forged", _system_task="late_session_critical_v1"))
    assert response.status_code == 429
    assert Rules(app_env["own"]).load_one("forged") is None


@pytest.mark.parametrize("lose_claim", [False, True])
def test_runtime_records_success_atomically_before_next_watchdog_tick(owned, monkeypatch, lose_claim):
    from app.strategy.monitor_rules import normalize

    sid = "ma_golden_cross"
    configured = configure(owned)
    source = owned.documents.get("strategy_sources", configured)
    owned.documents.delete("strategy_sources", configured)
    owned.documents.delete("critical_strategy_schedule", configured)
    owned.documents.put("strategy_sources", sid, source)
    owned.documents.put("critical_strategy_schedule", sid, {
        "enabled": True, "strategy_id": sid, "rule_id": "my_price", "name": "S02",
    })
    Rules(owned.documents).save_one(normalize(price_rule(
        conditions=[{"field": "csg_test", "op": "truth"}])))
    day = cn_today()
    owned.clock["now"] = datetime.combine(day, time(14, 30, 45), CN_TZ).timestamp()
    daily = {"as_of": str(day), "computed_at": int(owned.clock["now"] * 1000),
             "section_statuses": {"buy": "success"}, "sections": {"buy": []}}
    owned.worker.executor = SimpleNamespace(execute=lambda *_a, **_k: {
        "state": {}, "results": {sid: deepcopy(daily)}, "events": [], "execution_events": []})
    frame = owned.frame.with_columns(__import__("polars").lit(owned.clock["now"] * 1000).alias("quote_ts"))
    snapshot = owned.snapshot(frames={"stock": __import__(
        "app.multitenancy.market", fromlist=["encode_frame"]).encode_frame(frame)})
    key = f"{sid}:{day}"
    if lose_claim:
        checks = []
        def guard(*_args):
            checks.append(True)
            if len(checks) == 2:
                raise AccessError(409, "test lease expired before commit")
        monkeypatch.setattr(runtime, "valid_claim", guard)
        with pytest.raises(AccessError, match="test lease expired"):
            owned.worker.evaluate(owned.documents.context.tenant_id, snapshot)
        assert owned.documents.get("critical_strategy_runs", key) is None
        assert owned.documents.get("monitor_results", "default") is None
        assert Alerts(owned.documents).count() == 0
        return
    owned.worker.evaluate(owned.documents.context.tenant_id, snapshot)
    receipt = owned.documents.get("critical_strategy_runs", key)
    assert receipt["state"] == "succeeded" and receipt["buy_count"] == 0
    # A subsequent failure and parameter edit must not erase a completed run.
    owned.documents.put("monitor_results", "default", {"results": {}})
    owned.documents.put("strategy_overrides", sid, {"params": {"updated": True}})
    critical_schedule.check_completion(
        owned.worker, owned.documents, now=datetime.combine(day, time(14, 56), CN_TZ))
    assert owned.documents.get("critical_strategy_runs", key) == receipt
    assert Alerts(owned.documents).count() == 0


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
