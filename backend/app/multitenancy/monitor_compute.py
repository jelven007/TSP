"""Adapter around the original monitor engine for explicit public/private inputs."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import datetime

import polars as pl

from app.market_time import CN_TZ, cn_now, cn_today
from app.multitenancy.market import decode_frame
from app.strategy import custom_signals
from app.strategy.intraday_signals import IntradaySignalEvaluator
from app.strategy.monitor import MonitorRuleEngine


class SectorSnapshots:
    def __init__(self, values):
        self.values = {
            key: {**value, "window_changes": {int(k): v for k, v in value.get("window_changes", {}).items()}}
            for key, value in values.items()
        }

    def build_snapshots(self, _stock, _indices, targets, _windows, **_kwargs):
        return {target["key"]: self.values[target["key"]] for target in targets if target["key"] in self.values}


def failure_events(results, rules, previous, now):
    """Warnings and deduplication state are committed by the storage owner."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=CN_TZ)
    state = {key: copy.deepcopy(value) for key, value in previous.items()
             if value.get("as_of") == str(now.date())}
    events = []
    for sid, daily in results.items():
        rule = next((rule for rule in rules if rule.get("strategy_id") == sid), None)
        if rule is None:
            continue
        for kind, task in (daily.get("monitor_tasks") or {}).items():
            phases = []
            if task.get("signals"):
                for signal, saved in task["signals"].items():
                    steps = {p: saved[p] for p in ("breadth", "stocks") if p in saved}
                    phases.extend((signal, phase, value) for phase, value in
                                  (steps or {saved.get("phase", "observe"): saved}).items())
            else:
                phases.append(("", task.get("phase", kind), task))
            # A phase, retry or refreshed definition is still the same daily task.
            errors = list(dict.fromkeys(error for _, _, step in phases
                                        for error in step.get("errors") or []))
            successful = task.get("state") in ("success", "rejected")
            if not task.get("state"):
                successful = all(step.get("state") in ("success", "rejected")
                                 for _, _, step in phases)
            expired = task.get("state") == "expired" or all(
                step.get("state") == "expired" for _, _, step in phases)
            attempted = any(
                step.get("first_receipt", {}).get("state") not in (None, "expired")
                or any(value.get("state") != "expired" for value in step.get("history") or [])
                or (step.get("attempts", 0) > 1)
                for _, _, step in phases)
            if (expired and not attempted) or (not successful and not errors):
                continue
            rejected = successful and (task.get("state") == "rejected" or
                all(step.get("state") == "rejected" or step.get("passed") is False
                    for _, _, step in phases))
            outcome_state = "rejected" if rejected else "success" if successful else (
                "expired" if expired or task.get("terminal") else "failed")
            key = f"{sid}:task:{kind}"
            signature = hashlib.sha256(json.dumps(
                [sid, daily.get("as_of"), kind, outcome_state],
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if state.get(key, {}).get("signature") == signature:
                continue
            state[key] = {"as_of": str(now.date()), "signature": signature}
            label = {"buy": "买", "sell": "卖", "select": "选"}.get(kind, kind)
            outcome = ("计算成功, 条件未满足, 当日信号任务结束" if rejected
                       else "计算成功") if successful else (
                       "计算失败, 任务已结束" if outcome_state == "expired"
                       else "计算失败, 修复后在有效期限内重试")
            representative = {**phases[-1][2], **task}
            def diagnostics(value):
                if isinstance(value, dict):
                    return {key: diagnostics(item) for key, item in value.items() if key != "history"}
                if isinstance(value, list):
                    return [diagnostics(item) for item in value]
                return value
            details = {**diagnostics(copy.deepcopy(representative)),
                       "task": kind, "as_of": daily.get("as_of"), "state": outcome_state,
                       "errors": errors}
            phase_text = "; ".join(
                f"{signal or label}/{phase}: {'; '.join(step.get('errors') or []) or step.get('state')}"
                for signal, phase, step in phases)
            events.append({
                "id": hashlib.sha256(f"task-failed:{rule['id']}:{signature}".encode()).hexdigest(),
                "ts": int(now.timestamp() * 1000), "source": "system",
                "type": "signal_calculation_succeeded" if successful else "signal_calculation_failed",
                "strategy_id": sid,
                "rule_id": rule["id"], "rule_name": rule.get("name", ""),
                "name": label, "symbol": "", "severity": "info" if successful else "warning",
                "message": f"{rule.get('name', sid)} · {daily.get('as_of')} · {label}任务: "
                           f"{outcome}; {phase_text}; 尝试 {representative.get('attempts', 1)} 次; "
                           f"完整池 {representative.get('pool_total', '未提供')}, "
                           f"有效 {representative.get('pool_usable', '未提供')}, "
                           f"缺失 {representative.get('missing_count', '未提供')}; "
                           f"重试截止 {representative.get('retry_until') or '任务窗口结束'}; "
                           f"计划 {representative.get('scheduled_at') or '未提供'}, "
                           f"完成 {representative.get('completed_at') or '本次未完成'}",
                "details": details,
                "signals": [kind], "price": None, "change_pct": None,
            })
        for signal, quality in (daily.get("entry_data_quality") or {}).items():
            if (daily.get("monitor_tasks") or {}).get("buy", {}).get("signals"):
                continue
            progress = quality.get("progress") or {}
            failed = [(step, value.get("reason")) for step, value in progress.get("steps", {}).items()
                      if value.get("state") == "failed"]
            if not failed and progress.get("failure_reason"):
                failed = [(progress.get("phase", "stocks"), progress["failure_reason"])]
            if not failed:
                continue
            phase = progress.get("phase") or ("breadth" if failed[0][0] == "breadth" else "stocks")
            key = f"{sid}:task:buy"
            signature = hashlib.sha256(json.dumps(
                [sid, daily.get("as_of"), "buy",
                 "expired" if quality.get("stage") == "expired" else "failed"],
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if state.get(key, {}).get("signature") == signature:
                continue
            state[key] = {"as_of": str(now.date()), "signature": signature}
            events.append({
                "id": hashlib.sha256(f"signal-failed:{rule['id']}:{signature}".encode()).hexdigest(),
                "ts": int(now.timestamp() * 1000), "source": "system",
                "type": "signal_calculation_failed", "strategy_id": sid,
                "rule_id": rule["id"], "rule_name": rule.get("name", ""),
                "name": "尾盘走强", "symbol": "", "severity": "warning",
                "message": f"尾盘走强 · {daily.get('as_of')} · "
                           f"{'上涨占比' if phase == 'breadth' else '个股条件'}计算失败: "
                           f"{'; '.join(str(reason) for _, reason in failed)}; "
                           f"{'任务已结束' if quality.get('stage') == 'expired' else '在有效期限内持续重试'}",
                "details": {**copy.deepcopy(quality), "task": "buy", "as_of": daily.get("as_of")},
                "signals": [signal, phase], "price": None, "change_pct": None,
            })
    return events, state


def failed_task_results(payload, reason, now):
    """Retain the main task when context loading or the sandbox fails."""
    from app.strategy.late_session_strength import evaluate

    states = payload["state"].setdefault("engine", {}).setdefault("late_session_tasks", {})
    results = {}
    for sid, definitions in payload.get("late_task_definitions", {}).items():
        quality = {}
        scope = payload["late_task_scopes"][sid]
        for sig in definitions:
            column = "csgi_" + sig["id"]
            state = states.setdefault(f"{scope}:{column}", {})
            pool = payload.get("daily_pool_snapshots", {}).get(sid) or {}
            result = evaluate(
                None, sig, day=now.astimezone(CN_TZ).date(), pool_symbols=pool.get("symbols"),
                prev_close={}, completed_through=now, task_state=state, input_error=reason,
                selection=payload.get("selection_snapshots", {}).get(sid))
            quality[column] = {"stage": result.stage, "progress": result.progress}
        results[sid] = {"as_of": str(now.astimezone(CN_TZ).date()), "entry_data_quality": quality,
                        "rows": [], "total": 0, "entry_filter_error": reason}
    for sid in payload.get("strategy_task_ids", []):
        from app.services.strategy_monitor_tasks import window_state
        windows = payload.get("strategy_task_windows", {}).get(sid, {})
        tasks = {kind: {"state": "failed", "errors": [reason]}
                 for kind in ("buy", "sell") if window_state(windows, kind, now) == "running"}
        results.setdefault(sid, {"as_of": str(now.astimezone(CN_TZ).date()), "rows": [], "total": 0})
        results[sid]["monitor_tasks"] = tasks
    return results


def needs_sandbox(rules):
    return any(
        rule.get("type") == "strategy" or any(
            str(condition.get("field", "")).startswith(("csg_", "csgi_"))
            for condition in rule.get("conditions", [])
        ) for rule in rules
    )


def failed_phase_results(payload, reason, now):
    """Canonical receipts survive failures before a user strategy can return."""
    from app.services.strategy_monitor_state import CONTRACT, finished, receipt
    results = {}
    for sid in payload.get("strategy_task_ids", []):
        previous = payload.get("strategy_task_results", {}).get(sid) or {}
        daily = copy.deepcopy(previous if previous.get("as_of") == str(now.date()) else {})
        tasks = copy.deepcopy((payload.get("strategy_task_receipts", {}).get(sid) or {}).get("tasks", {}))
        for action in payload["strategy_task_plans"][sid]["actions"]:
            kind, signal_id, phase = action["kind"], action["signal_id"], action["phase"]
            task = tasks.setdefault(kind, {"signals": {}})
            saved = task.setdefault("signals", {}).get(signal_id, {})
            actual_phase = action.get("expired_phase", phase)
            nested = kind == "buy" and actual_phase in ("breadth", "stocks")
            step = receipt(saved.get(actual_phase, {}) if nested else saved, action, now, errors=[reason])
            task["signals"][signal_id] = ({**saved, actual_phase: step,
                **{key: step[key] for key in ("state", "status", "terminal", "errors")}}
                if nested else step)
            signals = list(task["signals"].values())
            configured = len(payload.get("strategy_task_windows", {}).get(sid, {}).get(kind, []))
            terminal = bool(configured and len(signals) == configured
                            and all(finished(value) for value in signals))
            task.update(state="expired" if terminal else "failed", terminal=terminal,
                        errors=[reason], retry_until=step["retry_until"])
            if kind == "buy":
                task["published"] = any(
                    (signal.get("stocks") or {}).get("state") in ("success", "rejected")
                    for signal in signals)
        daily.update(as_of=str(now.date()), monitor_contract=CONTRACT, monitor_tasks=tasks,
                     section_mode="signals", **payload["daily_identities"][sid])
        daily.setdefault("rows", [])
        daily.setdefault("total", len(daily["rows"]))
        daily.setdefault("sections", {"sell": (payload.get("sell_rosters", {}).get(sid) or {}).get("rows", []),
                                     "select": (payload.get("selection_snapshots", {}).get(sid) or {}).get("rows", []),
                                     "buy": daily["rows"]})
        for kind, task in tasks.items():
            daily.setdefault("section_statuses", {})[kind] = task["state"]
            daily.setdefault("section_errors", {})[kind] = "; ".join(task.get("errors", []))
        daily["execution_status"] = {"state": tasks.get("buy", {}).get("state", "waiting"), "reason": reason}
        results[sid] = daily
    return results


def fresh_frame(frame, now=None):
    field = "quote_ts" if "quote_ts" in frame.columns else "timestamp"
    if field not in frame.columns:
        return frame.head(0)
    stamp = pl.col(field).cast(pl.Float64, strict=False)
    seconds = pl.when(stamp > 1e12).then(stamp / 1000).otherwise(stamp)
    age = (time.time() if now is None else now) - seconds
    return frame.filter(age.is_between(-2, 15))


def evaluate_snapshot(payload, repo, strategy_engine=None, private_root=None):
    snapshot = payload["snapshot"]
    engine = MonitorRuleEngine(groups={key: frozenset(value) for key, value in payload.get("groups", {}).items()})
    previous = payload.get("state", {})
    engine.restore_state(previous.get("engine"))
    engine._late_session_tasks = {
        key: value for key, value in engine._late_session_tasks.items()
        if value.get("as_of") == str(cn_today())
    }
    engine.set_rules(payload["rules"])
    engine.set_name_map(snapshot.get("names", {}))
    if strategy_engine is not None:
        from app.services.screener import ScreenerService
        strategy_engine._daily_state_loader = lambda sid, day: payload.get("previous_states", {}).get(sid)
        strategy_engine._position_loader = lambda sid, day: payload.get("positions", {}).get(sid)
        strategy_engine._selection_loader = lambda sid, day: payload.get("selection_snapshots", {}).get(sid)
        strategy_engine._daily_pool_loader = lambda sid, day: payload.get("daily_pool_snapshots", {}).get(sid)
        strategy_engine._monitor_task_loader = lambda sid: payload.get("strategy_task_windows", {}).get(sid)
        strategy_engine._monitor_receipt_loader = lambda sid: payload.get("strategy_task_receipts", {}).get(sid, {})
        strategy_engine._monitor_result_loader = lambda sid: payload.get("strategy_task_results", {}).get(sid, {})
        strategy_engine._sell_roster_loader = lambda sid, day: payload.get("sell_rosters", {}).get(sid)

        engine.set_strategy_engine(strategy_engine)
        engine.set_data_dir(private_root)
        engine.set_history_loader(ScreenerService(repo)._load_enriched_history)
    engine.set_sector_monitor_service(SectorSnapshots(snapshot.get("sectors", {})))
    intraday = IntradaySignalEvaluator()
    intraday._last_bar = {
        tuple(key): datetime.fromisoformat(value) for key, value in previous.get("intraday", [])
    }
    events, execution_events, current_frames = [], [], {}

    def task_results():
        results = engine.latest_strategy_results()
        for sid in payload.get("strategy_task_ids", []):
            if sid not in results:
                results.update(failed_phase_results(
                    {**payload, "strategy_task_ids": [sid]},
                    engine._strategy_task_errors.get(sid, "计算未返回策略阶段回执"), cn_now()))
        for sid in payload.get("late_task_strategy_ids", []):
            if sid in results and sid not in engine._strategy_task_errors:
                continue
            failed_payload = {
                **payload, "state": {"engine": {"late_session_tasks": engine._late_session_tasks}},
                "late_task_definitions": {sid: payload.get("late_task_definitions", {}).get(sid, [])},
            }
            reason = engine._strategy_task_errors.get(sid, "本轮缺少可用的股票分钟计算上下文")
            failed = failed_task_results(failed_payload, reason, cn_now())[sid]
            results[sid] = {**failed, **results.get(sid, {}), "entry_data_quality": failed["entry_data_quality"]}
        return results

    if payload.get("late_task_only") and strategy_engine is not None:
        # Completed published minutes can be repaired without a new quote. This
        # path records calculation receipts, leaving live signal edges untouched.
        frame = decode_frame(snapshot["frames"].get("stock")) if snapshot["frames"].get("stock") else pl.DataFrame()
        for rule in payload["rules"]:
            sid = rule.get("strategy_id")
            if sid in payload.get("strategy_task_ids", []):
                matched = engine._match_strategy(frame, rule)
                for kind, symbol, name, price, change, signals in matched:
                    events.append({"type": kind, "symbol": symbol, "name": name,
                                   "price": price, "change_pct": change, "signals": signals,
                                   "strategy_id": sid, "rule_id": rule["id"],
                                   "rule_name": rule.get("name", ""), "source": "strategy",
                                   "severity": rule.get("severity", "info"),
                                   "ts": int(cn_now().timestamp() * 1000),
                                   "message": f"{rule.get('name', sid)} · {'需要卖出' if kind == 'sell_signal' else '买入信号'}"})
            elif sid in payload.get("late_task_strategy_ids", []):
                engine._match_strategy(frame, rule, emit_events=False)
        engine._latest_strategy_results = engine._building_strategy_results
        return {"events": events, "execution_events": [], "results": task_results(),
                "state": {"engine": engine.export_state(), "intraday": previous.get("intraday", [])}}
    if snapshot["status"].get("is_trading_hours"):
        for asset in ("stock", "index"):
            if snapshot.get("days", {}).get(asset) != cn_today().isoformat():
                continue
            # Preserve the worker's quote-age decision across sandbox startup.
            frame = fresh_frame(decode_frame(snapshot["frames"][asset]),
                                payload.get("evaluation_started_at"))
            if frame.is_empty():
                continue
            if strategy_engine is not None:
                expressions = custom_signals.build_expressions(
                    [item for item in payload.get("signals", []) if item.get("timeframe", "daily") != "intraday"],
                    allow_shift=False,
                )
                frame = custom_signals.inject(frame, expressions)
            symbols = engine.intraday_signal_symbols(asset)
            if symbols:
                # Read the platform's published minute partitions only. No
                # provider fallback is allowed in an API/tenant worker.
                minutes = repo.get_minute_batch(sorted(symbols), cn_today(), asset_type=asset)
                references = {}
                for row in frame.filter(pl.col("symbol").is_in(sorted(symbols))).iter_rows(named=True):
                    close, change = row.get("close"), row.get("change_pct")
                    reference = row.get("prev_close")
                    if reference is None and close is not None and change is not None and change > -1:
                        reference = close / (1 + change)
                    if reference is not None:
                        references[row["symbol"]] = reference
                signals = intraday.evaluate(
                    minutes, symbols=symbols, prev_close=references, asset_type=asset,
                    now=cn_now(), signals=[item for item in payload.get("signals", []) if item.get("timeframe") == "intraday"],
                )
                frame = intraday.inject(frame, signals)
            current_frames[asset] = frame
            events.extend(engine.evaluate(
                frame, asset_type=asset, reset_strategy_results=asset == "stock",
                execution_events=execution_events,
            ))
        events.extend(engine.evaluate_sectors(
            current_frames.get("stock", pl.DataFrame()), decode_frame(snapshot["indices"]),
        ))
        stock = current_frames.get("stock", pl.DataFrame())
        fresh_symbols = set(stock["symbol"].to_list()) if "symbol" in stock.columns else set()
        events.extend(engine.evaluate_abnormal([
            row for row in snapshot.get("abnormal", []) if row.get("symbol") in fresh_symbols
        ]))
        events.extend(engine.evaluate_date_rules())
    return {
        "events": events, "execution_events": execution_events, "results": task_results(),
        "state": {
            "engine": engine.export_state(),
            "intraday": [[list(key), value.isoformat()] for key, value in intraday._last_bar.items()],
        },
    }


def evaluate_strategy_phases(payload, repo, executor):
    """Trusted breadth IO/kernel runs independently of tenant code startup."""
    from dataclasses import asdict

    from app.services.screener import ScreenerService
    from app.services.strategy_monitor_state import finished, receipt
    from app.strategy.engine import StrategyDataContext
    from app.strategy.late_session_strength import evaluate, previous_closes

    output = {"events": [], "execution_events": [], "results": {}, "state": copy.deepcopy(payload["state"])}
    if not output["state"].get("engine"):
        output["state"]["engine"] = MonitorRuleEngine().export_state()
    states = output["state"].setdefault("engine", {}).setdefault("late_session_tasks", {})
    compute_ids = []
    for sid in payload["strategy_task_ids"]:
        actions = payload["strategy_task_plans"][sid]["actions"]
        sandbox_actions = [a for a in actions if a["phase"] in ("stocks", "observe", "expire")]
        if sandbox_actions:
            compute_ids.append(sid)
        for action in (a for a in actions if a["phase"] == "breadth"):
            now = cn_now()
            window = next(w for w in payload["strategy_task_windows"][sid]["buy"]
                          if w["signal"]["id"] == action["signal_id"])
            sig = window["signal"]
            column = sig.get("column") or "csgi_" + sig["id"]
            state = states.setdefault(f"{payload['late_task_scopes'][sid]}:{column}", {})
            pool = payload["daily_pool_snapshots"].get(sid) or {}
            error, references, minutes = None, {}, None
            try:
                symbols = pool.get("symbols")
                if symbols is None:
                    raise ValueError(pool.get("error") or "缺少该策略完整当日股票池")
                minutes = repo.get_minute_batch(symbols, now.date(), asset_type="stock")
                daily = ScreenerService(repo)._load_enriched_history(now.date(), 2)
                context = StrategyDataContext("stock", "1m", now.date(),
                                              daily_history=daily, current=pl.DataFrame())
                references = previous_closes(context, isolate_symbols=True)
            except Exception as exc:
                error = str(exc)
            calculated = evaluate(minutes, sig, day=now.date(), pool_symbols=pool.get("symbols"),
                                  prev_close=references, completed_through=now, task_state=state,
                                  input_error=error, breadth_only=True,
                                  selection=payload.get("selection_snapshots", {}).get(sid))
            completed = cn_now()
            quality = asdict(calculated)
            quality["missing_count"] = len(calculated.missing_symbols)
            quality["reference_errors"] = context.reference_errors if error is None else {}
            old = payload["strategy_task_receipts"].get(sid) or {}
            tasks = copy.deepcopy(old.get("tasks", {}))
            task = tasks.setdefault("buy", {"signals": {}})
            saved = task.setdefault("signals", {}).get(sig["id"], {})
            phase = receipt(saved.get("breadth", {}), action, completed,
                            passed=calculated.stage != "breadth_rejected",
                            errors=calculated.diagnostics, details=quality)
            signal = {**saved, "breadth": phase}
            if phase["state"] in ("rejected", "expired"):
                signal.update({k: phase[k] for k in ("state", "status", "terminal", "passed", "errors")})
            task["signals"][sig["id"]] = signal
            all_signals = [task["signals"].get(w["signal"]["id"], {}) for w in
                           payload["strategy_task_windows"][sid]["buy"]]
            terminal = all(finished(s) for s in all_signals)
            task.update({**quality, "signals": task["signals"],
                         "state": phase["state"] if terminal or phase["errors"] else "waiting",
                         "terminal": terminal, "passed": phase["passed"],
                         "attempts": phase["attempts"], "errors": phase["errors"],
                         "retry_until": phase["retry_until"],
                         "windows": [{k: w[k] for k in ("start", "end")} for w in
                                     payload["strategy_task_windows"][sid]["buy"]]})
            if terminal:
                task["completed_at"] = phase["completed_at"]
            previous = payload["strategy_task_results"].get(sid) or {}
            daily_result = copy.deepcopy(previous if previous.get("as_of") == str(now.date()) else {})
            selected = payload["selection_snapshots"].get(sid)
            daily_result.update(as_of=str(now.date()), rows=daily_result.get("rows", []),
                                total=daily_result.get("total", 0), section_mode="signals",
                                monitor_tasks=tasks, monitor_contract="strategy_monitor_v2",
                                asset_type=payload["daily_identities"][sid]["asset_type"],
                                timeframe=payload["daily_identities"][sid]["timeframe"],
                                runtime_identity=payload["daily_identities"][sid]["runtime_identity"])
            daily_result.setdefault("sections", {"sell": (payload["sell_rosters"].get(sid) or {}).get("rows", []),
                                                 "select": (selected or {}).get("rows", []), "buy": []})
            daily_result.setdefault("section_statuses", {})["buy"] = task["state"]
            daily_result.setdefault("entry_data_quality", {})[column] = quality
            daily_result["execution_status"] = {
                "state": "success" if terminal and not phase["errors"] else "failed" if phase["errors"] else "waiting",
                "reason": "上涨占比未达到阈值, 当日信号任务结束" if terminal else
                          "; ".join(phase["errors"]) if phase["errors"] else "上涨占比通过, 等待个股确认",
                "updated_at": int(completed.timestamp() * 1000)}
            output["results"][sid] = daily_result
            payload["strategy_task_receipts"][sid] = {**old, "tasks": tasks}
            payload["strategy_task_results"][sid] = daily_result
    if compute_ids:
        task_payload = {**payload, "state": output["state"], "late_task_only": True,
                        "strategy_task_ids": compute_ids,
                        "rules": [r for r in payload["rules"] if r.get("strategy_id") in compute_ids]}
        try:
            computed = executor.execute(task_payload)
        except Exception as exc:
            computed = {"events": [], "execution_events": [], "state": output["state"],
                        "results": failed_phase_results(task_payload, str(exc), cn_now())}
        missing = [sid for sid in compute_ids if sid not in computed.get("results", {})]
        if missing:
            computed["results"].update(failed_phase_results(
                {**task_payload, "strategy_task_ids": missing}, "计算未返回策略阶段回执", cn_now()))
        output["events"].extend(computed["events"])
        output["execution_events"].extend(computed.get("execution_events", []))
        output["results"].update(computed["results"])
        output["state"] = computed["state"]
    return output
