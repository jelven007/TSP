"""策略监控编排: 装配本侧输入、请求修复、汇合回执并守卫最终发布。"""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import polars as pl

from app.multitenancy.documents import TenantDocuments, json_value
from app.services.fs_utils import atomic_write_text
from app.services.strategy_monitor_state import (
    CONTRACT,
    finished,
    independent_buy,
    join_buy,
    plan,
    receipt,
)
from app.strategy import monitor_rules

NAMESPACE = "strategy_monitoring"
_local_lock = threading.RLock()

def assert_generation(storage, sid, generation):
    """提交前验证仍启用且世代相同, 防取消/重启后的在途结果迟到发布。"""
    current = monitoring_state(storage, sid)
    if not current.get("enabled") or current.get("generation") != generation:
        raise ValueError("策略监控已取消或重新开启, 旧任务结果已丢弃")


def retire_legacy(storage):
    """Migrate old automatic monitors once, with no source or archive deletion."""
    related = {rule["strategy_id"] for rule in monitor_rules.load_all(storage)
               if rule.get("type") == "strategy" and rule.get("strategy_id")
               and not rule.get("lot_id")}
    if isinstance(storage, TenantDocuments):
        related.update(row["id"] for row in storage.records("critical_strategy_schedule"))
    for sid in sorted(related):
        if monitoring_state(storage, sid).get("contract") != CONTRACT:
            set_monitoring(storage, sid, sid, False)


def _cancel_tasks(storage, sid):
    records = (storage.records("strategy_monitor_task_runs") if isinstance(storage, TenantDocuments) else
               [{"id": path.stem, "value": json.loads(path.read_text())} for path in
                (Path(storage) / "user_data" / "strategy_monitor_task_runs").glob("*.json")])
    for saved in records:
        value = saved["value"]
        if value.get("strategy_id") != sid:
            continue
        tasks = deepcopy(value.get("tasks", {}))
        changed = False
        for task in tasks.values():
            if not finished(task):
                task.update(state="cancelled", status="cancelled", terminal=True,
                            reason="策略监控已取消", next_at=None)
                changed = True
        if changed:
            task_receipt(storage, sid, value["as_of"], {**value, "tasks": tasks})


def monitoring_state(storage, sid):
    """读取监控生命周期及世代; 缺记录返回{}, 不视为默认启用。"""
    if isinstance(storage, TenantDocuments):
        return storage.get(NAMESPACE, sid, {})
    path = Path(storage) / "user_data" / NAMESPACE / f"{sid}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def task_receipt(storage, sid, day, value=None):
    """按策略和目标日读写回执, 不把前日失败并入今日尚未开始任务。"""
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


def request_input_repair(market, *, owner, sid, generation, day, phase, attempt,
                         symbols, deadline, minute_end=None, daily_dates=()):
    """向Platform请求本阶段成员和日期依赖; 接口失败返回修复错误, 租户不自行取数。"""
    request = {"owner": owner, "strategy_id": sid, "generation": generation,
               "day": str(day), "phase": phase, "attempt": attempt,
               "symbols": sorted(set(symbols)), "daily_dates": [str(value) for value in daily_dates],
               "minute_end": minute_end, "retry_until": deadline}
    try:
        if not request["symbols"]:
            raise ValueError("修复缺少完整范围身份, 不能用命中名单代替")
        value = market.monitor_repair(request)
        return {**value, "request": request}
    except Exception as exc:
        return {"state": "failed", "error": str(exc), "request": request}


def request_phase_repairs(payload, results, market, now):
    """为未完成且有错误的阶段提交补数, 原地把修复回执附到results。

    广度用完整池, 个股用已有冻结预选, 卖用前日买名单; 缺/空预选不造
    个股补数请求。修复目标仍是启动分钟及必要前日, 不沿当前行情滚动重选。
    """
    from app.services.strategy_input_binding import requires_authority
    from app.strategy.basic_filter import previous_session
    for sid, daily in results.items():
        if payload.get("s02_inputs_required") and sid == "custom_mvxxcp88":
            continue
        for kind, task in (daily.get("monitor_tasks") or {}).items():
            if kind == "buy" and requires_authority(payload, sid):
                continue
            for signal_id, saved in (task.get("signals") or {}).items():
                phases = {key: saved[key] for key in ("breadth", "stocks") if key in saved}
                for phase, step in (phases or {"observe": saved}).items():
                    if finished(step) or not step.get("errors"):
                        continue
                    window = next((w for w in payload["strategy_task_windows"][sid].get(kind, [])
                                   if w["signal"]["id"] == signal_id), None)
                    symbols = ((payload.get("sell_rosters", {}).get(sid) or {}).get("rows", [])
                               if kind == "sell" else None)
                    symbols = ([row["symbol"] for row in symbols] if symbols is not None else
                               (payload.get("daily_pool_snapshots", {}).get(sid) or {}).get("symbols", []))
                    if kind == "buy" and phase == "stocks":
                        selected = payload.get("selection_snapshots", {}).get(sid)
                        if selected is None or not selected.get("rows"):
                            # 无归档不能靠分钟修复造出预选; 成功空预选无需修复行情。
                            continue
                        symbols = sorted({row["symbol"] for row in selected["rows"]})
                    if not window:
                        continue
                    step["repair"] = request_input_repair(
                        market, owner=payload["tenant_id"], sid=sid,
                        generation=payload["strategy_task_generations"][sid], day=now.date(),
                        phase=phase, attempt=step["attempts"], symbols=symbols,
                        deadline=step["retry_until"],
                        minute_end=(window["start"] if kind == "buy" else
                                    now.replace(second=0, microsecond=0).strftime("%H:%M:%S")),
                        daily_dates=[previous_session(now.date())])
                    step["repair"]["input_release_id"] = payload.get("public_release_id")
                    if step.get("history"):
                        step["history"][-1]["repair"] = deepcopy(step["repair"])


def wait_for_repairs(planning, saved, market, *, current_release=None):
    """原地更新修复回执并移除暂缓动作, 过期动作保持可执行。

    同发布版本修复在途时暂缓计算; 新发布版本允许未完成阶段立即重新校验
    输入。修复成功只解除等待, 不能代替策略计算通过。
    """
    waiting = []
    for action in planning["actions"]:
        if action["phase"] == "expire":
            continue
        signal = saved.get("tasks", {}).get(action["kind"], {}).get("signals", {}).get(action["signal_id"], {})
        step = signal.get(action["phase"], {}) if action["phase"] in ("breadth", "stocks") else signal
        repair = step.get("repair") or {}
        if (current_release and repair.get("input_release_id")
                and current_release != repair["input_release_id"]):
            continue
        if repair.get("state") not in ("queued", "running") or not repair.get("request"):
            continue
        try:
            step["repair"] = {**repair, **market.monitor_repair(repair["request"])}
        except Exception as exc:
            step["repair"] = {**repair, "state": "failed", "error": str(exc)}
        if step["repair"].get("state") in ("queued", "running"):
            waiting.append(action)
    planning["actions"] = [action for action in planning["actions"] if action not in waiting]


def trusted_scope(engine, repo, storage, sid, day, overrides=None):
    """用证券目录和时点状态证据建立自身完整范围; 缺行情不缩池。

    覆盖合并后解析板块/分组; 目录不可用直接失败, 不能退回有行情子集。
    """
    from app.instrument_suspension import pool_evidence
    from app.strategy.daily_pool import build_pool
    from app.strategy.engine import StrategyDataContext
    from app.strategy.targets import resolve
    instruments = repo.get_instruments_asset("stock")
    if instruments is None or instruments.is_empty():
        raise ValueError("完整股票范围证券目录不可用")
    strategy = engine.get(sid)
    effective = {**(overrides or {}), **resolve(storage, {"basic_filter": {
        **(strategy.basic_filter or {}), **((overrides or {}).get("basic_filter") or {})}})}
    context = StrategyDataContext("stock", "1d", day,
        current=instruments.with_columns(pl.lit(day).alias("date")),
        pool_universe=pool_evidence(repo, day))
    return build_pool(engine, sid, context, overrides=effective, premarket=True)


def pending_selection_repairs(market, repairs):
    """查询在途预选补数并更新repairs, 返回仍需等待的策略ID集合。"""
    pending = set()
    for sid, repair in repairs.items():
        if repair.get("state") not in ("queued", "running") or not repair.get("request"):
            continue
        try:
            repairs[sid] = {**market.monitor_repair(repair["request"]), "request": repair["request"]}
        except Exception as exc:
            repairs[sid] = {**repair, "state": "failed", "error": str(exc)}
        if repairs[sid].get("state") in ("queued", "running"):
            pending.add(sid)
    return pending


def selection_repairs(market, repo, owner, generations, job, errors, *, engine=None,
                      storage=None, overrides=None):
    """按源日完整策略范围和声明的历史窗口补日线, 返回每策略修复回执。

    缺范围定义直接记录失败; 不用已有命中替代范围。后续预选仍需重新验证
    收盘证据、覆盖和资格, 此处不创建目标日快照或冻结ATR。
    """
    from app.strategy.basic_filter import previous_session
    repairs = {}
    source = date.fromisoformat(job["source_date"])
    for sid in errors:
        from app.services.selection_clock import S03_ID, window_text
        if sid == S03_ID and ("S02" in errors[sid] or "扩展" in errors[sid] or "财务" in errors[sid]):
            repairs[sid] = {"state": "waiting_data", "error": errors[sid]}
            continue
        try:
            if engine is None or storage is None:
                raise ValueError("修复缺少策略范围定义")
            symbols = trusted_scope(engine, repo, storage, sid, source,
                                    (overrides or {}).get(sid))["symbols"]
            bars = max(65 if sid == S03_ID else 1, engine.minute_daily_history_bars(
                [sid], overrides_map=overrides or {}))
            dates = [source]
            for _ in range(min(bars, 500) - 1):
                dates.append(previous_session(dates[-1]))
        except Exception as exc:
            repairs[sid] = {"state": "failed", "error": str(exc)}
            continue
        repairs[sid] = request_input_repair(
            market, owner=owner, sid=sid, generation=generations[sid],
            day=source, phase="select", attempt=job["attempts"],
            symbols=symbols, deadline=f"{source}T{window_text(sid)[1]}+08:00",
            daily_dates=sorted(dates))
    return repairs


def selection_receipts(storage, job, now):
    """将源日盘后作业转为各策略select回执, 保存覆盖与去重通知。

    目标日资格归档与源日任务回执日期不同; 作业错误按策略保留, 重复回执
    用作业签名识别。本函数写任务/通知, 不计算或补建资格。
    """
    from app.market_time import as_cn_datetime
    from app.multitenancy.monitor_compute import failure_events
    from app.services.alert_store import append_many
    now = as_cn_datetime(now)
    results = {}
    errors = job.get("errors") or {}
    for sid in set(job.get("completed") or []) | set(errors):
        from app.services.selection_clock import window_for, window_text
        start, end = window_for(sid)
        start_text, end_text = window_text(sid)
        old = task_receipt(storage, sid, job["source_date"])
        previous = old.get("tasks", {}).get("select", {})
        if finished(previous):
            if (not start <= now.time().replace(tzinfo=None) <= end
                    or (sid not in (job.get("selections") or {}) and sid not in errors)):
                continue
            previous = {**previous, "state": "failed", "terminal": False}
        source = str(job["source_date"])
        action = {"phase": "expire" if job.get("state") == "expired" else "select",
                  "expired_phase": "select", "scheduled_at": f"{source}T{start_text}+08:00",
                  "retry_until": f"{source}T{end_text}+08:00"}
        selected = (job.get("selections") or {}).get(sid) or {}
        coverage = selected.get("coverage") or {}
        from app.strategy.version import digest
        job_signature = digest(json_value({
            "job": {key: job.get(key) for key in ("started_at", "attempts", "state", "finished_at")},
            "error": errors.get(sid), "selection": {
                "as_of": selected.get("as_of"), "source_date": selected.get("source_date"),
                "count": len(selected.get("rows", [])), "coverage": coverage},
            "repair": (job.get("repairs") or {}).get(sid)}))
        if previous.get("job_signature") == job_signature:
            continue
        task = receipt(previous, action, now, passed=sid not in errors,
            errors=[errors[sid]] if sid in errors else [], details={
                "windows": [{"start": start_text, "end": end_text}],
                "source_date": source,
                "target_date": job["as_of"], "count": len(selected.get("rows", [])),
                "pool_total": coverage.get("scope_total"), "pool_usable": coverage.get("available_daily"),
                "missing_count": len(coverage.get("missing_symbols", [])),
                "invalid_symbols": coverage.get("invalid_symbols", {}),
                "candidate_total": coverage.get("candidate_total"),
                "scorable_total": coverage.get("scorable_total"),
                "warnings": selected.get("warnings", []),
                "repair": (job.get("repairs") or {}).get(sid), "job_signature": job_signature})
        results[sid] = {"as_of": job["source_date"], "monitor_tasks": {"select": task}}
        saved_receipt = {**old, "as_of": job["source_date"], "strategy_id": sid,
                   "tasks": {**old.get("tasks", {}), "select": task},
                   "contract": CONTRACT,
                   "updated_at": int(now.timestamp() * 1000)}
        alerts, alert_state = failure_events(
            {sid: results[sid]}, monitor_rules.load_all(storage), old.get("failures", {}), now)
        saved_receipt["failures"] = alert_state
        if isinstance(storage, TenantDocuments):
            from app.multitenancy.alerts import append_in_transaction
            append_in_transaction(storage.connection, storage.context.tenant_id, alerts)
        else:
            append_many(storage, alerts)
        task_receipt(storage, sid, job["source_date"], saved_receipt)


def monitoring_summary(engine, storage, *, now=None, strategy_id=None):
    from app.market_time import as_cn_datetime, cn_now, published_trading_day
    from app.services.daily_selections import DailySelections, due_window
    from app.strategy.config import list_overrides
    now = as_cn_datetime(now or cn_now())
    calendar = published_trading_day(now.date())
    active, overrides = monitored_ids(storage), list_overrides(storage)
    rows = []
    strategies = {strategy_id: engine.get(strategy_id)} if strategy_id else engine._strategies
    for sid, strategy in strategies.items():
        enabled = sid in active
        errors = []
        try:
            windows = task_windows(engine, strategy, overrides.get(sid) or {})
            signal_windows = task_windows(engine, strategy, overrides.get(sid) or {},
                                          apply_entry_cutoff=False)
        except ValueError as exc:
            windows = {"buy": [], "sell": []}
            signal_windows = windows
            errors.append(str(exc))
        saved = task_receipt(storage, sid, now.date()).get("tasks", {})
        tasks = {}
        for kind in ("buy", "sell"):
            state = window_state(windows, kind, now)
            receipt = saved.get(kind, {})
            if finished(receipt) or (state == "running" and receipt.get("state") == "failed"):
                state = receipt["state"]
            elif state == "expired" and receipt.get("state") == "success":
                state = "success"
            tasks[kind] = {**receipt, "state": state if enabled else "disabled",
                           "windows": [{k: w[k] for k in ("start", "end")} for w in signal_windows[kind]]}
            if any(w["end"] > "14:55:00" for w in signal_windows[kind]) and kind == "buy":
                tasks[kind]["execution_deadline"] = "14:55:00"
        from app.services.selection_clock import window_for, window_text
        start, end = window_for(sid)
        start_text, end_text = window_text(sid)
        select = {**saved.get("select", {}), "windows": [{"start": start_text, "end": end_text}]}
        select["state"] = "waiting"
        if published_trading_day(now.date()) is True:
            try:
                window = due_window(now.replace(hour=start.hour, minute=0, second=0, microsecond=0),
                                    monitored_only=True, strategy_id=sid)
                select["target_date"] = str(window[1])
                if (selected := DailySelections(storage).read(sid, window[1])) is not None:
                    select["state"] = "success"
                    select["count"] = len(selected["rows"])
                    from app.services.selection_clock import S03_ID
                    if sid == S03_ID:
                        sync = DailySelections(storage).job(str(window[1]) + ":s03").get("watchlist_sync", {})
                        if sync.get("state") == "failed":
                            select.update(state="failed", errors=[sync["error"]], terminal=False)
                elif now.time().replace(tzinfo=None) > end:
                    select["state"] = "expired"
                elif now.time().replace(tzinfo=None) >= start:
                    previous = saved.get("select", {})
                    select["state"] = "failed" if previous.get("errors") else "running"
                    select["terminal"] = False
            except ValueError as exc:
                select.update(state="failed", errors=[str(exc)])
        tasks["select"] = {**select, "state": select["state"] if enabled else "disabled"}
        if enabled and calendar is not True:
            for task in tasks.values():
                if task["state"] in {"waiting", "running"}:
                    task["state"] = "non_trading_day" if calendar is False else "calendar_unknown"
        from app.services.selection_clock import S03_ID
        grouped_entry = any((w["signal"].get("params") or {}).get("entry_groups") is not None
                            for w in windows["buy"])
        if sid == S03_ID and enabled and grouped_entry and not finished(saved.get("buy", {})):
            from app.strategy.overnight_runtime import next_market_day
            target = (now.date() if published_trading_day(now.date()) is True
                      and now.strftime("%H:%M:%S") <= max(w["end"] for w in windows["buy"])
                      else next_market_day(now.date()))
            if DailySelections(storage).read(sid, target) is None:
                source = (now.date() if published_trading_day(now.date()) is True
                          and now.time().replace(tzinfo=None) <= end else next_market_day(now.date()))
                effective = next_market_day(source)
                tasks["buy"].update(
                    state="blocked", target_date=str(effective),
                    dependency={"state": "missing_or_incompatible", "date": str(target),
                                "source_date": str(source), "kind": "s03_five_dimension_selection"},
                    errors=[f"{target}缺少当前S03五维预选归档, 买入暂不可执行; "
                            f"最早需{source}{start.strftime('%H:%M')}~{end.strftime('%H:%M')}"
                            f"完成评分, 供{effective}入场使用"])
        rows.append({"strategy_id": sid, "enabled": enabled, "as_of": str(now.date()),
                     "is_trading_day": calendar, "tasks": tasks, "errors": errors,
                     **({"operational_state": storage.get(
                         "strategy_operational_state", f"{sid}:{now.date()}")}
                        if sid == "custom_mvxxcp88" and isinstance(storage, TenantDocuments) else {})})
    return {"strategies": rows}


def monitored_ids(storage):
    return {rule["strategy_id"] for rule in monitor_rules.load_all(storage)
            if rule.get("type") == "strategy" and rule.get("enabled", True)
            and not rule.get("lot_id")
            and rule.get("strategy_id")
            and monitoring_state(storage, rule["strategy_id"]).get("enabled") is True}


def sell_roster(storage, sid, day):
    """读取当日卖关注名单, 优先次日转存槽, 再回读精确前日成功买归档。

    返回含rows的快照; 正常空归档rows=[]与缺证据None分别处理。
    这里的成员是买信号关注名单, 不据账户持仓或当前选股过滤改写。
    """
    from app.services.strategy_day_views import _get, _key
    from app.services.strategy_results import StrategyResults

    store = StrategyResults(storage)
    transaction = store.storage._transaction if store.tenant else store._local
    with transaction() as conn:
        if conn is None:
            return None
        saved = _get(store, conn, "strategy_sell_rosters", _key(sid, day, "stock", "1m"))
        if saved is not None and sid != "custom_mvxxcs05":
            return saved
        # 已有成功历史买档仍是卖名单证据, 不为恢复名单重算历史入场。
        from app.services.strategy_results import ResultIdentity
        identity = ResultIdentity(sid, "", "", "{}", "{}", "null", "stock", "1m")
        result = store._with_previous_buy(conn, identity, {
            "section_mode": "signals", "sections": {"sell": [], "select": [], "buy": []},
        }, str(day))
        if (result.get("section_errors") or {}).get("sell"):
            return None
        if result.get("sell_roster") is not None:
            return result["sell_roster"]
        from app.strategy.basic_filter import previous_session
        return {"strategy_id": sid, "as_of": str(day),
                "source_date": str(previous_session(date.fromisoformat(str(day)))),
                "rows": result["sections"]["sell"]}


def task_windows(engine, strategy, overrides, *, apply_entry_cutoff=True):
    """从有效信号形成保存窗口; 独立阶段和广度观测来自源码能力声明。"""
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
                start, end = params["time"] or "09:30:00", params.get("end_time") or "14:55:00"
            elif sig.get("template") == "morning_liquidation":
                from app.strategy.morning_liquidation import parameters
                params = parameters(sig)
                if all(value is None for value in params.values()):
                    continue
                start = params.get("start_time") or "09:30:00"
                end = params.get("fallback_time") or (
                    params["time"] if "amplitude_pct" in params else "23:59:59")
            elif sig.get("template") == "s01_late_entry":
                start, end = params.get("time"), "14:55:00"
            elif sig.get("template") == "s01_morning_exit":
                start, end = params.get("start_time"), params.get("fallback_time")
            else:
                start, end = params.get("start_time"), params.get("end_time") or params.get("fallback_time")
            if start and end:
                if task == "buy" and apply_entry_cutoff:
                    end = min(time.fromisoformat(end), time(14, 55)).isoformat()
                # 观测声明与下限分开: 空下限仍安排真实广度, 不因此增加买入条件。
                windows[task].append({"start": time.fromisoformat(start).isoformat(),
                                      "end": time.fromisoformat(end).isoformat(), "signal": sig,
                                      **({"phase_policy": "independent_v1"} if task == "buy"
                                         and sig.get("template") == "late_session_strength"
                                         and strategy.meta.get("entry_phase_policy") == "independent_v1" else {}),
                                      **({"observe_breadth": True} if task == "buy"
                                         and sig.get("template") == "late_session_strength"
                                         and strategy.meta.get("entry_breadth_observation") is True else {})})
    return windows


def strategy_windows(engine, strategy, storage, overrides):
    """读取已启用自定义策略的有效窗口; 沙箱以冻结loader为唯一来源。"""
    if getattr(strategy, "source", None) not in ("custom", "ai"):
        return None
    loader = getattr(engine, "_monitor_task_loader", None)
    if callable(loader):
        # Worker 持有监控生命周期及世代; 沙箱只读冻结窗口, 不能读取或启用
        # 租户监控文档。缺窗口保持未配置, 不能从默认信号重新启动任务。
        return loader(strategy.meta["id"]) or {"buy": [], "sell": []}
    if monitoring_state(storage, strategy.meta["id"]).get("enabled") is not True:
        return {"buy": [], "sell": []}
    return task_windows(engine, strategy, overrides)


def window_state(windows, kind, now):
    """按北京交易日和保存窗口描述时段, 不读取任务终态或证明计算成功。"""
    from app.market_time import as_cn_datetime, published_trading_day
    now = as_cn_datetime(now)
    spans = windows.get(kind) or []
    if not spans:
        return "unconfigured"
    if published_trading_day(now.date()) is not True:
        return "waiting"
    clock = now.time().replace(tzinfo=None)
    if any(time.fromisoformat(w["start"]) <= clock
           and (clock < time.fromisoformat(w["end"]) if kind == "sell"
                else clock <= time.fromisoformat(w["end"])) for w in spans):
        return "running"
    end = max(time.fromisoformat(w["end"]) for w in spans)
    return "expired" if clock > end or (kind == "sell" and clock == end) else "waiting"


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
    """提交前重查买/卖截止, 返回被拒绝事件方向并保留该次计算诊断。"""
    rejected = set()
    for kind, task in (daily.get("monitor_tasks") or {}).items():
        if daily.get("monitor_contract") == CONTRACT and kind == "buy":
            from app.market_time import as_cn_datetime
            closed = {w["signal"]["id"] for w in windows.get("buy", [])
                      if as_cn_datetime(now).time().replace(tzinfo=None) > time.fromisoformat(w["end"])}
            executed = (daily.get("monitor_executed_signals") or {}).get(
                kind, task.get("executed_signals", []))
            late = set(executed) & closed
            if independent_buy(windows.get("buy") or []):
                for signal_id in late:
                    signal = task.get("signals", {}).get(signal_id, {})
                    if signal.get("state") in ("success", "rejected") and signal.get("joined_at"):
                        signal.update(state="expired", status="failed", terminal=True,
                                      errors=["入场任务时段已结束, 迟到汇合结果已丢弃"])
                        rejected.add(kind)
                apply_independent_buy(daily, windows, now)
                continue
            if not late:
                continue
            reason = "入场任务时段已结束, 迟到结果已丢弃"
            for signal_id in late:
                signal = task.get("signals", {}).get(signal_id, {})
                step = signal.get("stocks") or {}
                if step.get("state") not in ("success", "rejected"):
                    continue
                step.update(state="expired", status="failed", terminal=True, errors=[reason])
                signal.update(state="expired", status="failed", terminal=True, errors=[reason])
                rejected.add(kind)
            if kind not in rejected:
                continue
            valid = [(signal.get("stocks") or {}) for signal in task.get("signals", {}).values()
                     if (signal.get("stocks") or {}).get("state") in ("success", "rejected")]
            rows = {row["symbol"]: row for step in valid for row in step.get("rows", [])}
            hits = {hit["symbol"]: hit for step in valid for hit in step.get("hits", [])}
            daily.update(rows=sorted(rows.values(), key=lambda row: (
                -(row.get("score") if isinstance(row.get("score"), (int, float)) else float("-inf")),
                row["symbol"])), total=len(rows), entry_signal_hits=list(hits.values()))
            daily["sections"]["buy"] = daily["rows"]
            task.update(state="expired", errors=[reason], published=bool(valid), count=len(rows))
            daily.setdefault("section_statuses", {})["buy"] = "expired"
            if not valid:
                daily.update(entry_filter_error=reason)
                daily.setdefault("section_errors", {})["buy"] = reason
            continue
        if daily.get("monitor_contract") == CONTRACT and kind == "sell":
            from app.market_time import as_cn_datetime
            closed = {w["signal"]["id"] for w in windows.get("sell", [])
                      if as_cn_datetime(now).time().replace(tzinfo=None) > time.fromisoformat(w["end"])}
            executed = set((daily.get("monitor_executed_signals") or {}).get(kind, []))
            late = {symbol for symbol, hit in task.get("new_triggered", {}).items()
                    if hit.get("monitor_signal_id") in closed & executed}
            if late:
                rejected.add(kind)
                reason = "出场任务时段已结束, 迟到触发结果已丢弃"
                task["triggered"] = {symbol: hit for symbol, hit in task.get("triggered", {}).items()
                                     if symbol not in late}
                daily["exit_signal_hits"] = list(task["triggered"].values())
                task.update(state="expired", errors=[reason], count=len(task["triggered"]))
                daily.setdefault("section_statuses", {})["sell"] = "expired"
                daily.setdefault("section_errors", {})["sell"] = reason
            continue
        if task.get("executed") is False:
            continue
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
    """卖页只消费前日成功买归档; 明确空归档成功为零, 缺归档保留等待原因。

    按所选出场逐股查首次信号, 异常合入 errors; 只在启用条件需要时读昨收。
    此入口不代表实际持仓已经卖出。
    """
    from app.market_time import as_cn_datetime
    from app.strategy.late_session_strength import previous_closes
    from app.strategy.morning_liquidation import evaluate, requires_previous_close

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
        needs_previous = any(window["signal"].get("template") != "morning_liquidation"
                             or requires_previous_close(window["signal"]) for window in windows["sell"])
        references = previous_closes(context, isolate_symbols=True) if needs_previous else {}
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
    """为保存卖名单独立加载分钟、日线及停牌证据, 不沿买入过滤缩小成员。

    指定的读取异常写exit_errors并保留其余输入; 更外层负责失败回执与重试。
    """
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


def apply_independent_buy(daily, windows, now):
    """原地汇合daily的独立买阶段, 返回同一daily; 本函数不写业务库。

    广度、个股和失败共用join_buy; 展示广度与最终买状态分别合并。
    保留原joined_at及已冻结名单, 空下限观测失败不改成买任务错误。
    """
    if not independent_buy(windows.get("buy") or []):
        return daily
    tasks = daily.setdefault("monitor_tasks", {})
    # stocks内部候选不能直接写最终买; 正常零与失败/未知也由同一join_buy判定。
    task, rows, hits = join_buy(tasks.get("buy") or {}, windows["buy"], now)
    tasks["buy"] = task
    daily.update(rows=rows, total=len(rows), entry_signal_hits=hits)
    daily.setdefault("sections", {})["buy"] = rows
    daily.setdefault("section_statuses", {})["buy"] = task["state"]
    errors = daily.setdefault("section_errors", {})
    # 仅展示广度的错误不写成买任务错误; 最终名单是否发布以join_buy为准。
    required = any(s.get("breadth_required") for s in task["signals"].values())
    reason = "; ".join(task["errors"]) or (
        None if task["published"] else "等待广度与冻结预选股独立计算完成" if required
        else "等待冻结预选股个股计算完成")
    daily["entry_filter_error"] = reason
    if reason:
        errors["buy"] = reason
    else:
        errors.pop("buy", None)
    quality = daily["entry_data_quality"] = daily.get("entry_data_quality") or {}
    for window in windows["buy"]:
        if window.get("phase_policy") != "independent_v1":
            continue
        value = task["signals"].get(window["signal"]["id"], {})
        breadth, stocks = value.get("breadth") or {}, value.get("stocks") or {}
        merged = {**quality.get(window["signal"].get("column") or "csgi_" + window["signal"]["id"], {}),
                  **{k: v for k, v in stocks.items() if k not in (
                      "rows", "hits", "history", "first_receipt", "errors", "progress")}}
        for key in ("pool_total", "pool_usable", "missing_pct", "missing_symbols", "missing_count",
                    "pool_up_pct", "pool_up_pct_lower", "pool_up_pct_upper", "known_positive",
                    "breadth_policy", "reference_errors"):
            if key in breadth:
                merged[key] = deepcopy(breadth[key])
        progress = deepcopy(stocks.get("progress") or breadth.get("progress") or {})
        progress.setdefault("steps", {})
        if (breadth.get("progress") or {}).get("steps", {}).get("breadth"):
            progress["steps"]["breadth"] = deepcopy(breadth["progress"]["steps"]["breadth"])
        progress["phase_policy"] = "independent_v1"
        progress["breadth_required"] = value.get("breadth_required")
        if value.get("joined_at"):
            progress["confirmed_at"] = value["joined_at"]
        merged.update(progress=progress, stage=value["state"])
        quality[window["signal"].get("column") or "csgi_" + window["signal"]["id"]] = merged
    daily["execution_status"] = {
        "state": "success" if task["state"] in ("success", "rejected") else task["state"],
        "reason": reason}
    return daily


def run_tasks(engine, strategy, context, overrides, windows, roster, *, exit_context=None):
    """执行本轮到期动作, 保存各方向/阶段回执, 再汇合最终买名单。

    完成侧不重算; 失败侧保留输入原因供 Platform 修复。卖使用独立退出
    上下文, 不能因买候选缺数据而抹掉可计算的卖信号或冻结选名单。
    """
    from app.market_time import published_trading_day
    from app.strategy.engine import StrategyResult
    from app.strategy.execution_status import status

    sid = strategy.meta["id"]
    saved_loader = getattr(engine, "_monitor_receipt_loader", None)
    saved = saved_loader(sid) if callable(saved_loader) else {}
    tasks = deepcopy((saved or {}).get("tasks", {}))
    if windows["sell"] and (tasks.get("sell") or {}).get("completion_reason") == "s05_no_daily_branch":
        tasks.pop("sell")
    from app.services.strategy_monitor_state import resume_confirmation_timeout
    tasks = resume_confirmation_timeout(windows, tasks, context.observed_at)
    if (windows["sell"] and published_trading_day(context.as_of) and roster is not None
            and roster.get("source_record_id") and roster.get("source_date")
            and roster.get("rows") == [] and not tasks.get("sell", {}).get("triggered")):
        old_sell = tasks.get("sell", {})
        if old_sell.get("completion_reason") != "empty_archived_sell_roster":
            completed = context.observed_at.isoformat()
            tasks["sell"] = {
                "state": "rejected", "status": "succeeded", "terminal": True,
                "errors": [], "count": 0, "completed_at": completed,
                "completion_reason": "empty_archived_sell_roster",
                "source_record_id": roster["source_record_id"], "source_date": roster["source_date"],
                "executed": False, "executed_signals": [],
                "signals": {w["signal"]["id"]: {
                    "state": "rejected", "status": "succeeded", "terminal": True,
                    "passed": False, "errors": [], "count": 0, "completed_at": completed,
                } for w in windows["sell"]},
                **({"prior_outcome": deepcopy(old_sell)} if old_sell else {}),
            }
    prior_loader = getattr(engine, "_monitor_result_loader", None)
    previous = prior_loader(sid) if callable(prior_loader) else {}
    result = StrategyResult(context.as_of, sid, [], 0)
    if previous and str(previous.get("as_of")) == str(context.as_of):
        for key in ("rows", "total", "entry_signal_hits", "exit_signal_hits", "entry_data_quality",
                    "sections", "section_mode", "section_statuses", "section_errors",
                    "selection_snapshot", "entry_filter_error", "execution_status"):
            if key in previous:
                setattr(result, key, deepcopy(previous[key]))
    actions = plan(windows, tasks, context.observed_at,
                   trading=published_trading_day(context.as_of))["actions"]
    stocks = [action for action in actions if action["kind"] == "buy" and action["phase"] == "stocks"]
    if stocks:
        columns = {window["signal"]["id"]: window["signal"].get("column") or "csgi_" + window["signal"]["id"]
                   for window in windows["buy"]}
        from app.strategy.version import runtime_identity
        context = replace(context, signal_task_scope=f"{sid}:{runtime_identity(strategy, overrides)}")
        buy = tasks.setdefault("buy", {"signals": {}})
        for action in stocks:
            window = next(w for w in windows["buy"] if w["signal"]["id"] == action["signal_id"])
            independent_phase = "stocks" if window.get("phase_policy") == "independent_v1" else None
            stock_context = replace(context, independent_entry_phase=independent_phase)
            effective = {**overrides, "entry_signals": [columns[action["signal_id"]]],
                         "exit_signals": []}
            try:
                if context.input_errors:
                    raise ValueError("; ".join(context.input_errors))
                calculated = engine.run(sid, stock_context, overrides=effective,
                                        params=dict(overrides.get("params") or {}))
            except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
                calculated = StrategyResult(context.as_of, sid, [], 0, entry_filter_error=str(exc))
            quality = (calculated.entry_data_quality or {}).get(columns[action["signal_id"]], {})
            errors = [calculated.entry_filter_error] if calculated.entry_filter_error else []
            old = buy.setdefault("signals", {}).get(action["signal_id"], {})
            step = receipt(old.get("stocks", {}), action, context.observed_at,
                           errors=errors, passed=bool(calculated.rows), details={
                               **quality, "rows": calculated.rows if not errors else [],
                               "hits": calculated.entry_signal_hits if not errors else [],
                               "count": len(calculated.rows) if not errors else 0})
            buy["signals"][action["signal_id"]] = {**old, "stocks": step,
                **({} if independent_phase else {key: step[key] for key in
                                                ("state", "terminal", "passed", "status", "errors")})}
            result.entry_data_quality = {**(result.entry_data_quality or {}),
                                         **(calculated.entry_data_quality or {})}
        # 每信号保留固定启动快照, 包括成功零命中; 后续行情不能重开。
        rows, hits = {}, {}
        for value in buy["signals"].values():
            completed = value.get("stocks") or {}
            if completed.get("state") not in ("success", "rejected"):
                continue
            for row in completed.get("rows") or []:
                rows.setdefault(row["symbol"], row)
            for hit in completed.get("hits") or []:
                symbol = hit["symbol"]
                if symbol in hits:
                    hits[symbol]["signals"] = sorted(set(hits[symbol].get("signals", []))
                                                    | set(hit.get("signals", [])))
                else:
                    hits[symbol] = deepcopy(hit)
        result.rows = sorted(rows.values(), key=lambda r: (
            -(r.get("score") if isinstance(r.get("score"), (int, float)) else float("-inf")),
            r["symbol"]))
        result.total, result.entry_signal_hits = len(rows), list(hits.values())
    for action in actions:
        if action["phase"] != "expire":
            continue
        task = tasks.setdefault(action["kind"], {"signals": {}})
        old = task.setdefault("signals", {}).get(action["signal_id"], {})
        phase = action["expired_phase"]
        prior_phase = old.get(phase, {}) if action["kind"] == "buy" else old
        step = receipt(prior_phase, action, context.observed_at, errors=[
            "上涨占比在买入有效时段内未计算成功" if action["expired_phase"] == "breadth"
            else "任务有效时段已结束, 未完成计算"])
        task["signals"][action["signal_id"]] = (
            {**old, phase: step, **({} if independent_buy(windows["buy"]) else {k: step[k] for k in
             ("state", "terminal", "passed", "status", "errors")})}
            if action["kind"] == "buy" else step)

    if result.sections is None:
        selected = getattr(engine, "_selection_loader", lambda *_: None)(sid, context.as_of)
        result.section_mode = "signals"
        result.selection_snapshot = selected
        result.sections = {"sell": [], "select": (selected or {}).get("rows", []), "buy": []}
        result.section_errors = {}
        result.section_statuses = {"select": "success" if selected is not None else "waiting"}
        if selected is None:
            result.section_errors["select"] = "盘后选股尚未生成"
    result.section_errors = result.section_errors or {}
    result.section_statuses = result.section_statuses or {}
    if windows.get("buy_no_market_route") and not result.rows and not result.entry_signal_hits:
        tasks["buy"] = deepcopy(windows["buy_no_market_route"])
    if windows.get("sell_no_market_route") and not tasks.get("sell", {}).get("triggered"):
        tasks["sell"] = deepcopy(windows["sell_no_market_route"])

    sell_actions = [a for a in actions if a["kind"] == "sell" and a["phase"] == "observe"]
    sell_task = tasks.setdefault("sell", {})
    if sell_task.get("triggered"):
        result.exit_signal_hits = list(deepcopy(sell_task["triggered"]).values())
    if sell_actions:
        sell_task["new_triggered"] = {}
        triggered = deepcopy(sell_task.get("triggered", {}))
        for action in sell_actions:
            active_windows = {**windows, "sell": [
                w for w in windows["sell"] if w["signal"]["id"] == action["signal_id"]]}
            allowed = active_windows["sell"][0].get("symbols")
            scoped = [r for r in (roster or {}).get("rows", [])
                      if allowed is None or r["symbol"] in allowed]
            remaining = ({**roster, "rows": [r for r in scoped if r["symbol"] not in triggered]}
                         if roster is not None else None)
            evaluated = evaluate_sell_task(engine, strategy, exit_context or context, overrides,
                                           active_windows, remaining)
            for hit in evaluated["hits"]:
                if hit["symbol"] not in triggered:
                    triggered[hit["symbol"]] = {**hit, "monitor_signal_id": action["signal_id"]}
                    sell_task["new_triggered"][hit["symbol"]] = triggered[hit["symbol"]]
            count = sum(r["symbol"] in triggered for r in scoped)
            done = roster is not None and count == len(scoped)
            old = sell_task.setdefault("signals", {}).get(action["signal_id"], {})
            sell_task["signals"][action["signal_id"]] = receipt(
                old, action, context.observed_at, errors=evaluated["errors"],
                passed=bool(count), complete=done, details={
                    "count": count, "remaining_count": len(scoped) - count,
                    "hits": evaluated["hits"]})
        sell_task["triggered"] = triggered
        result.exit_signal_hits = list(triggered.values())

    for kind in ("buy", "sell"):
        task = tasks.setdefault(kind, {})
        if kind == "buy" and independent_buy(windows["buy"]):
            tasks["buy"], result.rows, result.entry_signal_hits = join_buy(task, windows["buy"], context.observed_at)
            result.total = len(result.rows)
            continue
        if finished(task):
            continue
        task["executed"] = any(a["kind"] == kind for a in actions)
        task["executed_signals"] = [a["signal_id"] for a in actions
                                   if a["kind"] == kind and a["phase"] in ("stocks", "observe")]
        task["windows"] = [{k: w[k] for k in ("start", "end")} for w in windows[kind]]
        signals = list((task.get("signals") or {}).values())
        configured = len(windows[kind])
        terminal = bool(configured and len(signals) == configured and all(finished(s) for s in signals))
        states = {s.get("state") for s in signals}
        state = ("unconfigured" if not configured else
                 "expired" if terminal and "expired" in states else
                 "success" if terminal and "success" in states else
                 "rejected" if terminal else "failed" if "failed" in states else
                 "running" if any(a["kind"] == kind for a in actions) else "waiting")
        task.update(state=state, terminal=terminal, status="succeeded" if terminal and state in
                    ("success", "rejected") else "failed" if terminal else "queued",
                    errors=list(dict.fromkeys(error for s in signals for error in s.get("errors", []))),
                    count=len(result.exit_signal_hits) if kind == "sell" else len(result.rows))
        if "branch_code" in windows:
            task["branch_code"] = windows["branch_code"]
        if terminal:
            task["completed_at"] = max(s.get("completed_at") or
                (s.get("stocks") or {}).get("completed_at") or context.observed_at.isoformat() for s in signals)
    buy_state = tasks["buy"]["state"]
    result.section_statuses["buy"] = buy_state
    result.section_statuses["sell"] = tasks["sell"]["state"]
    completed_buy = any((s.get("stocks") or {}).get("state") in ("success", "rejected")
                        for s in tasks["buy"].get("signals", {}).values())
    if not independent_buy(windows["buy"]):
        tasks["buy"]["published"] = completed_buy or buy_state in ("success", "rejected")
    if tasks["buy"]["published"]:
        result.entry_filter_error = None
        result.section_errors.pop("buy", None)
        result.sections["buy"] = result.rows
    elif tasks["buy"].get("errors"):
        result.entry_filter_error = "; ".join(tasks["buy"]["errors"])
        result.section_errors["buy"] = result.entry_filter_error
    elif not result.entry_filter_error:
        result.entry_filter_error = ("入场触发器未配置有效时段" if buy_state == "unconfigured" else
            "入场信号时段已结束" if buy_state == "expired" else "等待入场阶段计算")
        result.section_errors["buy"] = result.entry_filter_error
    if "sell_source_error" in windows:
        tasks["sell"]["errors"] = list(dict.fromkeys([
            *tasks["sell"].get("errors", []), windows["sell_source_error"]]))
        if not windows["sell"]:
            tasks["sell"].update(state="waiting", terminal=False, status="queued")
        elif tasks["sell"].get("terminal"):
            tasks["sell"].update(state="failed", status="failed")
        result.section_statuses["sell"] = tasks["sell"]["state"]
    result.sections["sell"] = (roster or {}).get("rows", [])
    result.section_errors.pop("sell", None)
    if tasks["sell"].get("errors"):
        result.section_errors["sell"] = "; ".join(tasks["sell"]["errors"])
    result.exit_filter_error = result.section_errors.get("sell")
    result.monitor_tasks = tasks
    result.monitor_contract = CONTRACT
    result.monitor_executed_signals = {kind: [a["signal_id"] for a in actions
        if a["kind"] == kind and a["phase"] in ("stocks", "observe")] for kind in ("buy", "sell")}
    result.execution_status = status("success" if buy_state in ("success", "rejected") else
                                    "failed" if buy_state == "failed" else
                                    "expired" if buy_state == "expired" else "waiting",
                                    result.entry_filter_error)
    if independent_buy(windows["buy"]):
        daily = apply_independent_buy(asdict(result), windows, context.observed_at)
        for key in ("rows", "total", "entry_signal_hits", "entry_data_quality", "sections",
                    "section_statuses", "section_errors", "entry_filter_error", "monitor_tasks", "execution_status"):
            setattr(result, key, daily[key])
    return result


def run_monitored(engine, strategy, loader, storage, day, current, overrides, windows, task_states):
    """按未完成动作装配输入; 个股读取失败转回执, 不拖累独立卖或完整池广度。

    非分组尾盘只读取有效冻结预选的成员分钟; 正常空预选无需市场上下文。
    """
    from app.market_time import cn_now
    from app.strategy.engine import StrategyDataContext
    from app.strategy.overnight_runtime import monitor_context
    now = cn_now()
    saved_loader = getattr(engine, "_monitor_receipt_loader", None)
    saved = saved_loader(strategy.meta["id"]) if callable(saved_loader) else {}
    from app.market_time import published_trading_day
    actions = plan(windows, (saved or {}).get("tasks", {}), now,
                   trading=published_trading_day(day))["actions"]
    roster_loader = getattr(engine, "_sell_roster_loader", None)
    roster = (roster_loader(strategy.meta["id"], day) if callable(roster_loader)
              else sell_roster(storage, strategy.meta["id"], day))
    independent = (sell_context(loader.__self__, day, current, roster, now)
                   if any(a["kind"] == "sell" and a["phase"] == "observe" for a in actions)
                   else StrategyDataContext("stock", "1m", day, current=current, observed_at=now))
    context = independent
    if any(a["kind"] == "buy" and a["phase"] == "stocks" for a in actions):
        try:
            from app.services.selection_clock import S05_ID
            from app.strategy.late_session_strength import optional_definition
            sid = strategy.meta["id"]
            due_ids = {a["signal_id"] for a in actions if a["kind"] == "buy" and a["phase"] == "stocks"}
            selected_only = all(w["signal"].get("template") == "late_session_strength"
                                and optional_definition(w["signal"])
                                and (w["signal"].get("params") or {}).get("entry_groups") is None
                                for w in windows["buy"] if w["signal"]["id"] in due_ids)
            frozen_loader = getattr(engine, "_frozen_input_loader", None)
            frozen = frozen_loader(strategy.meta["id"]) if callable(frozen_loader) else None
            if frozen is not None:
                context = frozen
            elif selected_only or sid == S05_ID:
                selection_loader = getattr(engine, "_selection_loader", None)
                if callable(selection_loader):
                    selected = selection_loader(strategy.meta["id"], day)
                else:
                    from app.services.daily_selections import DailySelections
                    selected = DailySelections(storage).read(sid, day, live_entry=True)
                context = loader.__self__.build_selected_stock_context(
                    engine, day, strategy.meta["id"], selected, current=current,
                    overrides=overrides, observed_at=now)
            else:
                context = monitor_context(loader, engine, strategy.meta["id"], day, current, storage)
        except Exception as exc:
            context = StrategyDataContext(
                "stock", "1m", day, current=current, observed_at=now, realtime=True,
                input_errors=[f"买任务行情读取失败: {exc}"])
    context.signal_task_states = task_states
    return run_tasks(engine, strategy, context, overrides, windows, roster, exit_context=independent)


def forward_buy(store, conn, identity, saved):
    """在归档所属写事务内把正常买名单转存下一交易日卖关注槽。

    正常零名单也转存以证明次日为空; 等待、失败、仅预选归档均不转存。
    保存源记录ID与买入日期供回读, 不写持仓或交易账本。
    """
    from app.services.strategy_day_views import _get, _key, _put, publish
    from app.strategy.overnight_runtime import next_market_day
    result = saved["result"]
    if identity.strategy_id == "custom_mvxxcs05":
        from app.strategy.s02_173004552 import project_daily_buys
        result = project_daily_buys(result)
    published = (result.get("monitor_contract") == CONTRACT
                 and (result.get("monitor_tasks") or {}).get("buy", {}).get("published"))
    if (result.get("selection_only") or result.get("sell_roster_only")
            or result.get("entry_filter_error") or result.get("execution_error")
            or (result.get("section_errors") or {}).get("buy")
            or (not published and (result.get("section_statuses") or {}).get(
                "buy", "waiting") not in ("success", "rejected"))):
        return
    day = str(next_market_day(date.fromisoformat(result["as_of"])))
    bought = result["sections"]["buy"]
    if identity.strategy_id == "custom_mvxxcs05":
        from app.strategy.s02_173004552 import with_entry_branches

        bought = with_entry_branches(bought, result.get("entry_signal_hits") or [])
    roster = {
        "strategy_id": identity.strategy_id, "as_of": day, "source_date": result["as_of"],
        "source_record_id": saved["record_id"],
        "strategy_version": saved["summary"]["strategy_version"],
        "rows": [{**row, "entry_date": result["as_of"], "entry_record_id": saved["record_id"]}
                 for row in bought],
    }
    key = _key(identity.strategy_id, day, identity.asset_type, identity.timeframe)
    if _get(store, conn, "strategy_sell_rosters", key) != roster:
        _put(store, conn, "strategy_sell_rosters", key, roster)
    publish(store, conn, identity.strategy_id, day, identity.asset_type, identity.timeframe, cascade=False)


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
            chosen = related[0] if related else None
            for rule in related:
                rules.save_one({**rule, "enabled": False}, system=True)
            if enabled:
                chosen = monitor_rules.normalize({
                    **(chosen or {}), "id": (chosen or {}).get("id", "mr_" + uuid4().hex[:20]),
                    "name": f"策略监控 · {name}", "type": "strategy", "strategy_id": sid,
                    "enabled": True, "asset_type": "stock", "scope": "all", "symbols": [],
                    "direction": "both", "notify_events": ["buy_signal", "sell_signal"],
                    "conditions": [], "cooldown_seconds": 0,
                })
                chosen.pop("_system_task", None)
                monitor_rules.validate(chosen)
                rules.save_one(chosen, system=True)
            critical = unit.get("critical_strategy_schedule", sid)
            if critical:
                unit.put("critical_strategy_schedule", sid, {**critical, "enabled": False,
                         "retired_by": CONTRACT})
            state = {"strategy_id": sid, "enabled": enabled,
                     "contract": CONTRACT,
                     "generation": old.get("generation", 0) + 1,
                     "rule_id": chosen["id"] if chosen else None}
            unit.put(NAMESPACE, sid, state)
            if not enabled:
                _cancel_tasks(unit, sid)
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
                chosen.pop("_system_task", None)
                monitor_rules.save_one(storage, chosen)
            state = {"strategy_id": sid, "enabled": enabled,
                     "contract": CONTRACT,
                     "generation": old.get("generation", 0) + 1,
                     "rule_id": chosen["id"] if chosen else None}
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(state))
            if not enabled:
                _cancel_tasks(storage, sid)
            return state
        except Exception:
            if chosen and chosen["id"] not in {rule["id"] for rule in related}:
                monitor_rules.delete_one(storage, chosen["id"])
            for rule in changed:
                monitor_rules.save_one(storage, rule)
            if previous is not None:
                atomic_write_text(path, previous)
            raise
