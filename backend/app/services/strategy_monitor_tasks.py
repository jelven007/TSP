"""Strategy-level monitoring lifecycle over the existing rules and scheduler."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import polars as pl

from app.multitenancy.documents import TenantDocuments
from app.services.fs_utils import atomic_write_text
from app.strategy import monitor_rules

NAMESPACE = "strategy_monitoring"
_local_lock = threading.RLock()


def monitoring_state(storage, sid):
    if isinstance(storage, TenantDocuments):
        return storage.get(NAMESPACE, sid, {})
    path = Path(storage) / "user_data" / NAMESPACE / f"{sid}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def task_receipt(storage, sid, day, value=None):
    key = f"{sid}:{day}"
    if isinstance(storage, TenantDocuments):
        return (storage.get("strategy_monitor_task_runs", key, {}) if value is None
                else storage.put("strategy_monitor_task_runs", key, value))
    path = Path(storage) / "user_data" / "strategy_monitor_task_runs" / f"{key}.json"
    if value is None:
        return json.loads(path.read_text()) if path.exists() else {}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, ensure_ascii=False))
    return value


def selection_receipts(storage, job, now):
    """Persist independent nightly state and deduplicated warnings."""
    from app.market_time import as_cn_datetime
    from app.multitenancy.monitor_compute import failure_events
    from app.services.alert_store import append_many
    now = as_cn_datetime(now)
    results = {}
    errors = job.get("errors") or {}
    for sid in set(job.get("completed") or []) | set(errors):
        old = task_receipt(storage, sid, job["source_date"])
        task = {"state": ("expired" if job.get("state") == "expired" else "failed")
                        if sid in errors else "success",
                "windows": [{"start": "20:00:00", "end": "22:00:00"}],
                "target_date": job["as_of"], "errors": [errors[sid]] if sid in errors else []}
        results[sid] = {"as_of": job["source_date"], "monitor_tasks": {"select": task}}
        receipt = {**old, "as_of": job["source_date"], "strategy_id": sid,
                   "tasks": {**old.get("tasks", {}), "select": task},
                   "updated_at": int(now.timestamp() * 1000)}
        alerts, alert_state = failure_events(
            {sid: results[sid]}, monitor_rules.load_all(storage), old.get("failures", {}), now)
        receipt["failures"] = alert_state
        if isinstance(storage, TenantDocuments):
            from app.multitenancy.alerts import append_in_transaction
            append_in_transaction(storage.connection, storage.context.tenant_id, alerts)
        else:
            append_many(storage, alerts)
        task_receipt(storage, sid, job["source_date"], receipt)


def monitoring_summary(engine, storage, *, now=None):
    from app.market_time import as_cn_datetime, cn_now, published_trading_day
    from app.services.daily_selections import DailySelections, due_window, signal_strategy
    from app.strategy.config import list_overrides
    now = as_cn_datetime(now or cn_now())
    active, overrides = monitored_ids(storage), list_overrides(storage)
    rows = []
    for sid, strategy in engine._strategies.items():
        if strategy.source not in ("custom", "ai") or not signal_strategy(strategy):
            continue
        enabled = sid in active
        errors = []
        try:
            windows = task_windows(engine, strategy, overrides.get(sid) or {})
        except ValueError as exc:
            windows = {"buy": [], "sell": []}
            errors.append(str(exc))
        saved = task_receipt(storage, sid, now.date()).get("tasks", {})
        tasks = {}
        for kind in ("buy", "sell"):
            state = window_state(windows, kind, now)
            receipt = saved.get(kind, {})
            if state == "running" and receipt.get("state") in ("success", "failed"):
                state = receipt["state"]
            elif state == "expired" and receipt.get("state") == "success":
                state = "success"
            tasks[kind] = {**receipt, "state": state if enabled else "disabled",
                           "windows": [{k: w[k] for k in ("start", "end")} for w in windows[kind]]}
        select = {**saved.get("select", {}), "windows": [{"start": "20:00:00", "end": "22:00:00"}]}
        select["state"] = "waiting"
        if published_trading_day(now.date()) is True:
            try:
                window = due_window(now.replace(hour=20, minute=0, second=0), monitored_only=True)
                select["target_date"] = str(window[1])
                if DailySelections(storage).read(sid, window[1]) is not None:
                    select["state"] = "success"
                elif now.time().replace(tzinfo=None) > time(22):
                    select["state"] = "expired"
                elif now.time().replace(tzinfo=None) >= time(20):
                    select["state"] = saved.get("select", {}).get("state", "running")
            except ValueError as exc:
                select.update(state="failed", errors=[str(exc)])
        tasks["select"] = {**select, "state": select["state"] if enabled else "disabled"}
        rows.append({"strategy_id": sid, "enabled": enabled, "as_of": str(now.date()),
                     "tasks": tasks, "errors": errors})
    return {"strategies": rows}


def monitored_ids(storage):
    return {rule["strategy_id"] for rule in monitor_rules.load_all(storage)
            if rule.get("type") == "strategy" and rule.get("enabled", True)
            and not rule.get("lot_id")
            and rule.get("strategy_id")
            and monitoring_state(storage, rule["strategy_id"]).get("enabled", True)}


def sell_roster(storage, sid, day):
    """A confirmed signal list, distinct from positions and from a missing receipt."""
    from app.services.strategy_day_views import _get, _key
    from app.services.strategy_results import StrategyResults

    store = StrategyResults(storage)
    transaction = store.storage._transaction if store.tenant else store._local
    with transaction() as conn:
        if conn is None:
            return None
        saved = _get(store, conn, "strategy_sell_rosters", _key(sid, day, "stock", "1m"))
        if saved is not None:
            return saved
        # Existing successful historical buy archives remain valid roster evidence.
        from app.services.strategy_results import ResultIdentity
        identity = ResultIdentity(sid, "", "", "{}", "{}", "null", "stock", "1m")
        result = store._with_previous_buy(conn, identity, {
            "section_mode": "signals", "sections": {"sell": [], "select": [], "buy": []},
        }, str(day))
        if (result.get("section_errors") or {}).get("sell"):
            return None
        from app.strategy.basic_filter import previous_session
        return {"strategy_id": sid, "as_of": str(day),
                "source_date": str(previous_session(date.fromisoformat(str(day)))),
                "rows": result["sections"]["sell"]}


def task_windows(engine, strategy, overrides):
    from app.strategy import custom_signals
    from app.strategy.s01_signals import optional_triggers, stored_definitions, trigger_definitions

    if optional_triggers(strategy):
        definitions = trigger_definitions(engine, strategy, overrides, strict=False)
    else:
        stored = stored_definitions(engine)
        definitions = {
            kind: [{**sig, "column": custom_signals.intraday_column_name(sig["id"])}
                   for sig in stored if custom_signals.intraday_column_name(sig["id"]) in
                   engine._effective_signals(overrides, key, getattr(strategy, key))]
            for kind, key in (("entry", "entry_signals"), ("exit", "exit_signals"))
        }
    windows = {"buy": [], "sell": []}
    for kind, task in (("entry", "buy"), ("exit", "sell")):
        for sig in definitions[kind]:
            if sig.get("enabled") is not True:
                continue
            params = sig.get("params") or {}
            if sig.get("template") == "late_session_strength":
                from app.strategy.late_session_strength import parameters
                params = parameters(sig)
                start, end = params["time"], params.get("end_time")
            elif sig.get("template") == "morning_liquidation":
                from app.strategy.morning_liquidation import parameters
                params = parameters(sig)
                if all(value is None for value in params.values()):
                    continue
                start = params.get("start_time") or "09:30:00"
                end = params.get("fallback_time") or params.get("time") or "11:30:00"
            elif sig.get("template") == "s01_late_entry":
                start, end = params.get("time"), "14:55:00"
            elif sig.get("template") == "s01_morning_exit":
                start, end = params.get("start_time"), params.get("fallback_time")
            else:
                start, end = params.get("start_time"), params.get("end_time") or params.get("fallback_time")
            if start and end:
                windows[task].append({"start": time.fromisoformat(start).isoformat(),
                                      "end": time.fromisoformat(end).isoformat(), "signal": sig})
    return windows


def strategy_windows(engine, strategy, storage, overrides):
    from app.strategy.signal_sections import supports_signal_sections
    if getattr(strategy, "source", None) not in ("custom", "ai") or not supports_signal_sections(strategy):
        return None
    if monitoring_state(storage, strategy.meta["id"]).get("enabled") is False:
        return {"buy": [], "sell": []}
    loader = getattr(engine, "_monitor_task_loader", None)
    return loader(strategy.meta["id"]) if callable(loader) else task_windows(engine, strategy, overrides)


def window_state(windows, kind, now):
    from app.market_time import as_cn_datetime, published_trading_day
    now = as_cn_datetime(now)
    spans = windows.get(kind) or []
    if not spans:
        return "unconfigured"
    if published_trading_day(now.date()) is not True:
        return "waiting"
    clock = now.time().replace(tzinfo=None)
    if any(time.fromisoformat(w["start"]) <= clock <= time.fromisoformat(w["end"]) for w in spans):
        return "running"
    return "expired" if clock > max(time.fromisoformat(w["end"]) for w in spans) else "waiting"


def next_task_check(windows, now, *, interval=30):
    from app.market_time import as_cn_datetime, published_trading_day
    now = as_cn_datetime(now)
    candidates = [now + timedelta(seconds=interval)]
    if published_trading_day(now.date()) is True:
        for spans in windows.values():
            for span in spans:
                for key in ("start", "end"):
                    boundary = datetime.combine(now.date(), time.fromisoformat(span[key]), now.tzinfo)
                    if boundary > now:
                        candidates.append(boundary)
    return min(candidates)


def fence_tasks(daily, windows, now):
    """Discard newly computed signal edges after their saved task window."""
    rejected = set()
    for kind, task in (daily.get("monitor_tasks") or {}).items():
        state = window_state(windows, kind, now)
        if task.get("state") != "success" or state == "running":
            continue
        rejected.add(kind)
        reason = f"{'入场' if kind == 'buy' else '出场'}任务时段已结束, 迟到结果已丢弃"
        task.update(state=state, errors=[reason])
        daily.setdefault("section_statuses", {})[kind] = state
        daily.setdefault("section_errors", {})[kind] = reason
        if kind == "buy":
            daily.update(rows=[], total=0, entry_filter_error=reason)
            daily["sections"]["buy"] = []
            from app.strategy.execution_status import status
            daily["execution_status"] = status(state, reason)
        else:
            daily.update(exit_signal_hits=[], exit_filter_error=reason)
    return rejected


def evaluate_sell_task(engine, strategy, context, overrides, windows, roster):
    from app.market_time import as_cn_datetime
    from app.strategy.late_session_strength import previous_closes
    from app.strategy.morning_liquidation import evaluate

    now = as_cn_datetime(context.observed_at)
    state = window_state(windows, "sell", now)
    if state != "running":
        return {"state": state, "hits": [], "errors": []}
    if roster is None:
        return {"state": "waiting", "hits": [], "errors": ["缺少前交易日买入归档, 卖出名单待确认"]}
    if not roster["rows"]:
        return {"state": "success", "hits": [], "errors": []}
    hits, errors = [], list(context.exit_errors)
    try:
        references = previous_closes(context, isolate_symbols=True)
    except ValueError as exc:
        references = {}
        errors.append(str(exc))
    for window in windows["sell"]:
        if not window["start"] <= now.time().replace(tzinfo=None).isoformat() <= window["end"]:
            continue
        sig = window["signal"]
        try:
            if sig.get("template") == "morning_liquidation":
                evaluated = evaluate(
                    context.history, sig, day=context.as_of, now=now, positions=None,
                    roster=roster["rows"], prev_close=references, quotes=context.current,
                    suspension_history=context.suspension_history)
                errors.extend(evaluated.diagnostics)
                hits.extend({**hit, "signals": [sig.get("column") or "csgi_" + sig["id"]]}
                            for hit in evaluated.hits)
            else:
                from app.strategy.s01_signals import optional_exit_hits
                ctx = replace(context, signal_roster=roster["rows"], exit_errors=[])
                hits.extend(optional_exit_hits(engine, strategy, ctx,
                    engine.resolve_params(strategy, None, overrides), [sig], overrides=overrides))
                errors.extend(ctx.exit_errors)
        except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
            errors.append(str(exc))
    earliest = {}
    for hit in sorted(hits, key=lambda h: h.get("trigger_time") or ""):
        earliest.setdefault(hit["symbol"], {**hit, "signal_roster": True})
    return {"state": "failed" if errors else "success", "hits": list(earliest.values()), "errors": errors}


def sell_context(service, day, current, roster, now):
    """Load the complete saved sell list independently of current selection filters."""
    from app.strategy.engine import StrategyDataContext
    symbols = [row["symbol"] for row in (roster or {}).get("rows", [])]
    context = StrategyDataContext("stock", "1m", day, current=current, observed_at=now,
                                  completed_through=now, realtime=True)
    try:
        context.history = service.repo.get_minute_batch(symbols, day, asset_type="stock") if symbols else pl.DataFrame()
    except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
        context.history = pl.DataFrame()
        context.exit_errors.append(f"卖出分钟数据读取失败: {exc}")
    try:
        context.daily_history = service._load_enriched_history(day, 2)
    except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
        context.daily_history = pl.DataFrame()
        context.exit_errors.append(f"卖出日线数据读取失败: {exc}")
    try:
        context.suspension_history = getattr(service.repo, "get_suspension_history", lambda: None)()
    except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
        context.exit_errors.append(f"卖出停牌证据读取失败: {exc}")
    return context


def run_tasks(engine, strategy, context, overrides, windows, roster, *, exit_context=None):
    from app.strategy.engine import StrategyResult
    from app.strategy.execution_status import status

    buy_state = window_state(windows, "buy", context.observed_at)
    if buy_state == "running":
        try:
            if context.input_errors:
                raise ValueError("; ".join(context.input_errors))
            result = engine.run(strategy.meta["id"], context, overrides=overrides,
                                params=dict(overrides.get("params") or {}))
        except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
            buy_state = "failed"
            result = StrategyResult(context.as_of, strategy.meta["id"], [], 0,
                                    entry_filter_error=str(exc))
    else:
        result = StrategyResult(context.as_of, strategy.meta["id"], [], 0)
        result.entry_filter_error = (
            "入场触发器未配置有效时段" if buy_state == "unconfigured" else
            "入场信号时段已结束" if buy_state == "expired" else "等待入场触发器启动")
    if result.sections is None:
        selected = getattr(engine, "_selection_loader", lambda *_: None)(strategy.meta["id"], context.as_of)
        result.section_mode = "signals"
        result.selection_snapshot = selected
        result.sections = {"sell": [], "select": (selected or {}).get("rows", []), "buy": []}
        result.section_errors = {"buy": result.entry_filter_error}
        result.section_statuses = {"buy": buy_state, "select": "success" if selected is not None else "waiting"}
        if selected is None:
            result.section_errors["select"] = "盘后选股尚未生成"
    sell = evaluate_sell_task(engine, strategy, exit_context or context, overrides, windows, roster)
    result.exit_signal_hits = sell["hits"]
    result.sections["sell"] = (roster or {}).get("rows", [])
    result.section_statuses["sell"] = sell["state"]
    result.section_errors.pop("sell", None)
    if sell["errors"]:
        result.section_errors["sell"] = "; ".join(sell["errors"])
    result.exit_filter_error = result.section_errors.get("sell")
    result.monitor_tasks = {
        "buy": {"state": result.section_statuses["buy"], "windows": [
            {k: w[k] for k in ("start", "end")} for w in windows["buy"]],
                "errors": [result.entry_filter_error] if result.section_statuses["buy"] == "failed"
                          and result.entry_filter_error else []},
        "sell": {"state": sell["state"], "windows": [
            {k: w[k] for k in ("start", "end")} for w in windows["sell"]],
                 "count": len(result.exit_signal_hits), "errors": sell["errors"]},
    }
    if result.section_statuses["buy"] != "success":
        result.execution_status = status(
            "failed" if result.section_statuses["buy"] == "failed" else
            "expired" if result.section_statuses["buy"] == "expired" else "waiting",
            result.entry_filter_error)
    return result


def run_monitored(engine, strategy, loader, storage, day, current, overrides, windows, task_states):
    from app.market_time import cn_now
    from app.strategy.engine import StrategyDataContext
    from app.strategy.overnight_runtime import monitor_context
    now = cn_now()
    roster_loader = getattr(engine, "_sell_roster_loader", None)
    roster = (roster_loader(strategy.meta["id"], day) if callable(roster_loader)
              else sell_roster(storage, strategy.meta["id"], day))
    independent = sell_context(loader.__self__, day, current, roster, now)
    context = independent
    if window_state(windows, "buy", now) == "running":
        try:
            context = monitor_context(loader, engine, strategy.meta["id"], day, current, storage)
        except Exception as exc:
            context = StrategyDataContext(
                "stock", "1m", day, current=current, observed_at=now, realtime=True,
                input_errors=[f"买任务行情读取失败: {exc}"])
    context.signal_task_states = task_states
    return run_tasks(engine, strategy, context, overrides, windows, roster, exit_context=independent)


def forward_buy(store, conn, identity, saved):
    from app.services.strategy_day_views import _get, _key, _put, publish
    from app.strategy.overnight_runtime import next_market_day
    result = saved["result"]
    if (result.get("selection_only") or result.get("sell_roster_only")
            or result.get("entry_filter_error") or result.get("execution_error")
            or (result.get("section_errors") or {}).get("buy")
            or (result.get("section_statuses") or {}).get("buy", "success") != "success"):
        return
    day = str(next_market_day(date.fromisoformat(result["as_of"])))
    roster = {
        "strategy_id": identity.strategy_id, "as_of": day, "source_date": result["as_of"],
        "source_record_id": saved["record_id"],
        "strategy_version": saved["summary"]["strategy_version"],
        "rows": [{**row, "entry_date": result["as_of"], "entry_record_id": saved["record_id"]}
                 for row in result["sections"]["buy"]],
    }
    key = _key(identity.strategy_id, day, "stock", "1m")
    if _get(store, conn, "strategy_sell_rosters", key) != roster:
        _put(store, conn, "strategy_sell_rosters", key, roster)
    publish(store, conn, identity.strategy_id, day, cascade=False)


def set_monitoring(storage, sid, name, enabled):
    """One transaction fences prior compute and all legacy recovery sources."""
    if not monitor_rules.ID_RE.fullmatch(sid):
        raise ValueError("策略ID非法")
    if isinstance(storage, TenantDocuments):
        from app.multitenancy.events import append_event
        from app.multitenancy.monitor_rules import Rules

        with storage.transaction() as unit:
            rules = Rules(unit)
            old = unit.get(NAMESPACE, sid, {})
            related = [rule for rule in rules.load_all()
                       if rule.get("type") == "strategy" and rule.get("strategy_id") == sid
                       and not rule.get("lot_id")]
            chosen = next((rule for rule in related if rule.get("_system_task")),
                          related[0] if related else None)
            for rule in related:
                # Only this strategy-level operation may stop its legacy critical rule.
                rules.save_one({**rule, "enabled": False}, system=True)
            if enabled:
                chosen = monitor_rules.normalize({
                    **(chosen or {}), "id": (chosen or {}).get("id", "mr_" + uuid4().hex[:20]),
                    "name": f"策略监控 · {name}", "type": "strategy", "strategy_id": sid,
                    "enabled": True, "asset_type": "stock", "scope": "all", "symbols": [],
                    "direction": "both", "notify_events": ["buy_signal", "sell_signal"],
                    "conditions": [], "cooldown_seconds": 0,
                })
                monitor_rules.validate(chosen)
                rules.save_one(chosen, system=True)
            critical = unit.get("critical_strategy_schedule", sid)
            if critical:
                unit.put("critical_strategy_schedule", sid, {**critical, "enabled": enabled})
            state = {"strategy_id": sid, "enabled": enabled,
                     "generation": old.get("generation", 0) + 1,
                     "rule_id": chosen["id"] if chosen else None}
            unit.put(NAMESPACE, sid, state)
            append_event(unit.connection, unit.context.tenant_id, "strategy_results_updated",
                         {"strategy_ids": [sid], "monitoring": enabled})
            return state
    with _local_lock:
        old = monitoring_state(storage, sid)
        related = [rule for rule in monitor_rules.load_all(storage)
                   if rule.get("type") == "strategy" and rule.get("strategy_id") == sid
                   and not rule.get("lot_id")]
        chosen = related[0] if related else None
        changed = []
        path = Path(storage) / "user_data" / NAMESPACE / f"{sid}.json"
        previous = path.read_text() if path.exists() else None
        try:
            for rule in related:
                monitor_rules.save_one(storage, {**rule, "enabled": False})
                changed.append(rule)
            if enabled:
                chosen = monitor_rules.normalize({
                    **(chosen or {}), "id": (chosen or {}).get("id", "mr_" + uuid4().hex[:20]),
                    "name": f"策略监控 · {name}", "type": "strategy", "strategy_id": sid,
                    "enabled": True, "scope": "all", "symbols": [], "direction": "both",
                    "notify_events": ["buy_signal", "sell_signal"], "cooldown_seconds": 0,
                })
                monitor_rules.save_one(storage, chosen)
            state = {"strategy_id": sid, "enabled": enabled,
                     "generation": old.get("generation", 0) + 1,
                     "rule_id": chosen["id"] if chosen else None}
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(state))
            return state
        except Exception:
            if chosen and chosen["id"] not in {rule["id"] for rule in related}:
                monitor_rules.delete_one(storage, chosen["id"])
            for rule in changed:
                monitor_rules.save_one(storage, rule)
            if previous is not None:
                atomic_write_text(path, previous)
            raise
