"""Recover explicitly enabled system schedules through existing monitor fencing."""
# ruff: noqa: RUF001 -- localized operational alerts.
from app.multitenancy.monitor_rules import Rules
from app.multitenancy.research import authorize
from app.strategy import monitor_rules

SYSTEM_TASK = "late_session_critical_v1"


def active_signals(unit, schedule):
    if not schedule.get("enabled"):
        return []
    sid = schedule["strategy_id"]
    source = unit.get("strategy_sources", sid)
    if not source:
        return []
    definition = source.get("definition", {})
    override = unit.get("strategy_overrides", sid, {})
    columns = override.get("entry_signals", definition.get("entry_signals", [])) or []
    return [
        signal for signal in unit.values("custom_signals")
        if "csgi_" + signal["id"] in columns and signal.get("enabled", True)
        and signal.get("template") == "late_session_strength"
    ]


def completed_buy(daily, day, confirmation, deadline):
    computed_at = daily.get("computed_at")
    return (
        daily.get("as_of") == day
        and (daily.get("section_statuses") or {}).get("buy") == "success"
        and not daily.get("selection_only") and not daily.get("showing_last_success")
        and (daily.get("execution_status") or {}).get("state", "success") == "success"
        and isinstance(computed_at, (int, float))
        and confirmation.timestamp() * 1000 <= computed_at <= deadline.timestamp() * 1000
    )


def record_completion(unit, sid, daily, *, now, fingerprint):
    """Commit the daily receipt with the realtime result, before a later retry can replace it."""
    from datetime import datetime, time

    from app.market_time import CN_TZ, as_cn_datetime
    from app.strategy.late_session_strength import evaluation_time

    now = as_cn_datetime(now)
    signals = active_signals(unit, unit.get("critical_strategy_schedule", sid, {}))
    if not signals:
        return
    confirmation = max(evaluation_time(signal, now.date()) for signal in signals).replace(tzinfo=CN_TZ)
    deadline = datetime.combine(now.date(), time(14, 55), CN_TZ)
    day = str(now.date())
    if now > deadline or not completed_buy(daily, day, confirmation, deadline):
        return
    key = f"{sid}:{day}"
    previous = unit.get("critical_strategy_runs", key, {})
    if previous.get("state") == "succeeded":
        return
    unit.put("critical_strategy_runs", key, {
        **previous, "strategy_id": sid, "as_of": day, "state": "succeeded",
        "confirmation_at": confirmation.isoformat(), "deadline_at": deadline.isoformat(),
        "checked_at": now.isoformat(), "fingerprint": fingerprint,
        "completed_at": daily["computed_at"], "runtime_identity": daily.get("runtime_identity"),
        "buy_count": len((daily.get("sections") or {}).get("buy") or []),
    })


def reconcile(worker, documents, claim=None):
    from app.multitenancy.runtime import valid_claim

    schedules = [row for row in documents.records("critical_strategy_schedule") if row["value"].get("enabled")]
    if not schedules:
        return
    account, context = worker.account(documents.context.tenant_id)
    with authorize(worker.identity, context, account["auth_version"]), documents.transaction() as unit:
        valid_claim(unit.connection, claim)
        rules = Rules(unit)
        for row in unit.records("critical_strategy_schedule"):
            schedule = row["value"]
            if not schedule.get("enabled"):
                continue
            sid = schedule["strategy_id"]
            if not active_signals(unit, schedule):
                continue
            saved = rules.load_one(schedule["rule_id"])
            execution = (saved or {}).get("_execution", {})
            expected_execution = {
                "user_id": context.user_id, "auth_version": account["auth_version"],
                "policy_version": context.policy_version, "routing_epoch": context.routing_epoch,
            }
            if (saved and saved.get("enabled") and saved.get("strategy_id") == sid
                    and saved.get("_system_task") == SYSTEM_TASK
                    and execution == expected_execution):
                continue
            rule = monitor_rules.normalize({
                **(saved or {}), "id": schedule["rule_id"], "name": "系统关键任务 · " + schedule["name"],
                "type": "strategy", "strategy_id": sid, "enabled": True,
                "asset_type": "stock", "scope": "all", "symbols": [], "direction": "both",
                "notify_events": ["buy_signal", "sell_signal"], "conditions": [],
                "logic": "or", "cooldown_seconds": 3600, "webhook_enabled": False,
                "webhook_channels": [], "_system_task": SYSTEM_TASK,
            })
            monitor_rules.validate(rule)
            rules.save_one(rule, system=True)


def check_completion(worker, documents, claim=None, *, now=None):
    """Observe completion independently of realtime compute; never replay a buy."""
    import hashlib
    from datetime import datetime, time, timedelta

    from app.market_time import CN_TZ, as_cn_datetime, cn_now, published_trading_day
    from app.multitenancy.alerts import append_in_transaction
    from app.multitenancy.runtime import fingerprint, valid_claim
    from app.strategy.late_session_strength import evaluation_time

    now = as_cn_datetime(now or cn_now())
    if published_trading_day(now.date()) is not True:
        return
    if not any(row["value"].get("enabled")
               for row in documents.records("critical_strategy_schedule")):
        return
    account, context = worker.account(documents.context.tenant_id)
    with authorize(worker.identity, context, account["auth_version"]), documents.transaction() as unit:
        valid_claim(unit.connection, claim)
        observed = fingerprint(unit.connection, include_state=False)
        monitored = unit.get("monitor_results", "default", {})
        day = str(now.date())
        deadline = datetime.combine(now.date(), time(14, 55), CN_TZ)
        for row in unit.records("critical_strategy_schedule"):
            schedule = row["value"]
            signals = active_signals(unit, schedule)
            if not signals:
                continue
            confirmation = max(evaluation_time(signal, now.date()) for signal in signals).replace(tzinfo=CN_TZ)
            if now < confirmation + timedelta(seconds=60) and now <= deadline:
                continue
            sid = schedule["strategy_id"]
            key = f"{sid}:{day}"
            previous = unit.get("critical_strategy_runs", key, {})
            # A later edit must not erase the fact that today's earlier definition ran.
            if previous.get("state") == "succeeded":
                continue
            daily = (monitored.get("results") or {}).get(sid) or {}
            completed = (
                monitored.get("fingerprint") == observed
                and completed_buy(daily, day, confirmation, deadline)
            )
            state = "succeeded" if completed else "expired" if now > deadline else "delayed"
            if state == previous.get("state"):
                continue
            receipt = {
                **previous, "strategy_id": sid, "as_of": day, "state": state,
                "confirmation_at": confirmation.isoformat(), "deadline_at": deadline.isoformat(),
                "checked_at": now.isoformat(), "fingerprint": observed,
            }
            if completed:
                receipt.update(completed_at=daily["computed_at"], runtime_identity=daily.get("runtime_identity"),
                               buy_count=len((daily.get("sections") or {}).get("buy") or []))
            else:
                message = (
                    "买入确认任务未完成，实时新入场已截止；请检查数据、计算服务及任务状态"
                    if state == "expired" else
                    "买入确认任务未完成，已超过确认时间60秒；实时任务将在截止前继续重试"
                )
                append_in_transaction(unit.connection, context.tenant_id, [{
                    "id": hashlib.sha256(f"critical:{context.tenant_id}:{key}:{state}".encode()).hexdigest(),
                    "source": "system", "type": f"critical_task_{state}", "strategy_id": sid,
                    "rule_id": schedule["rule_id"], "ts": int(now.timestamp() * 1000),
                    "name": schedule["name"], "severity": "critical" if state == "expired" else "warn",
                    "message": f"{schedule['name']} · {day} · {message}",
                }], channels=[])
            unit.put("critical_strategy_runs", key, receipt)
        valid_claim(unit.connection, claim)
