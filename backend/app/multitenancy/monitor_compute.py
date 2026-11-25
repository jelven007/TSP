"""监控阶段入口: 按执行器选择隔离编排, 可信广度与沙箱失败各自形成回执。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
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
    """从真实任务回执生成结果通知及按日去重状态, 返回值由存储所有者提交。

    空下限广度观测不生成买任务失败通知; 未实际尝试的纯过期不伪造计算
    失败。相同策略/日期/方向/终态只通知一次, 修复重试不按每次错误刷屏。
    """
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
                    steps = {p: saved[p] for p in ("breadth", "stocks") if p in saved
                             and not (kind == "buy" and p == "breadth"
                                      and (saved.get("breadth_required") is False
                                           or saved[p].get("observation_only") is True))}
                    phases.extend((signal, phase, value) for phase, value in
                                  (steps or {saved.get("phase", "observe"): saved}).items())
            else:
                phases.append(("", task.get("phase", kind), task))
            if not phases:
                continue
            # 子阶段、重试或定义刷新仍归同一策略当日方向, 不扩大通知去重粒度。
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
    """兼容监控路径把上下文/沙箱错误转为按策略诊断, 不宣称成功零名单。"""
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
    """沙箱未正常返回也生成本阶段失败回执; 保留先前正常阶段及最终汇合边界。"""
    from app.services.strategy_monitor_state import CONTRACT, finished, receipt
    from app.services.strategy_monitor_tasks import apply_independent_buy
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
            independent = any(w.get("phase_policy") == "independent_v1"
                              and w["signal"]["id"] == signal_id
                              for w in payload["strategy_task_windows"][sid].get("buy", []))
            task["signals"][signal_id] = ({**saved, actual_phase: step,
                **({} if independent else {key: step[key] for key in ("state", "status", "terminal", "errors")})}
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
        daily["sections"] = daily.get("sections") or {
            "sell": (payload.get("sell_rosters", {}).get(sid) or {}).get("rows", []),
            "select": (payload.get("selection_snapshots", {}).get(sid) or {}).get("rows", []),
            "buy": daily["rows"]}
        daily["section_statuses"] = daily.get("section_statuses") or {}
        daily["section_errors"] = daily.get("section_errors") or {}
        for kind, task in tasks.items():
            daily.setdefault("section_statuses", {})[kind] = task["state"]
            daily.setdefault("section_errors", {})[kind] = "; ".join(task.get("errors", []))
        daily["execution_status"] = {"state": tasks.get("buy", {}).get("state", "waiting"), "reason": reason}
        daily["monitor_executed_signals"] = {kind: [a["signal_id"]
            for a in payload["strategy_task_plans"][sid]["actions"]
            if a["kind"] == kind and a.get("expired_phase", a["phase"]) == "stocks"
            and not a.get("observation_only")] for kind in ("buy", "sell")}
        results[sid] = apply_independent_buy(daily, payload["strategy_task_windows"][sid], now)
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
    """沙箱计算冻结快照; 租户定义/预选/任务窗口只来自本次 payload。

    不读租户业务库或取上游行情; 返回结果和内部状态, 由 Worker 守卫提交。
    该入口服务个股/卖及普通监控, 可信广度另有不加载策略源码的入口。
    """
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
        from app.services.strategy_input_binding import frozen_context, requires_authority
        strategy_engine._daily_state_loader = lambda sid, day: payload.get("previous_states", {}).get(sid)
        strategy_engine._position_loader = lambda sid, day: payload.get("positions", {}).get(sid)
        strategy_engine._selection_loader = lambda sid, day: payload.get("selection_snapshots", {}).get(sid)
        strategy_engine._daily_pool_loader = lambda sid, day: payload.get("daily_pool_snapshots", {}).get(sid)
        strategy_engine._monitor_task_loader = lambda sid: payload.get("strategy_task_windows", {}).get(sid)
        strategy_engine._monitor_receipt_loader = lambda sid: payload.get("strategy_task_receipts", {}).get(sid, {})
        strategy_engine._monitor_result_loader = lambda sid: payload.get("strategy_task_results", {}).get(sid, {})
        strategy_engine._sell_roster_loader = lambda sid, day: payload.get("sell_rosters", {}).get(sid)
        def frozen_input(sid):
            if not requires_authority(payload, sid):
                return None
            manifest = payload.get("strategy_input_manifests", {}).get(sid, {}).get("stocks")
            if (manifest is None or manifest["input"]["strategy_id"] != sid
                    or manifest["phase"] != "stocks"):
                raise ValueError("权威输入未同步, 禁止使用独立行情")
            return frozen_context(manifest)
        strategy_engine._frozen_input_loader = frozen_input

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
        # 按日任务可以消费已发布分钟重试, 不要求新报价心跳;
        # 回执和首次通知沿任务状态处理, 不改普通监控的报价上升沿。
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
            # 用Worker开始观察时钟判断报价年龄, 保留沙箱启动前的输入决策。
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
                # 只读Platform已发布分钟, API/租户Worker缺数据时不回退Provider。
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
    """按executor.isolated选择Broker容器编排或本进程顺序适配。

    返回阶段结果、事件及引擎状态, 不在计算侧归档或写租户业务库。
    """
    if getattr(executor, "isolated", False):
        from app.multitenancy.monitor_phase_processes import evaluate_processes
        return evaluate_processes(payload, repo, executor)
    return evaluate_strategy_phases_serial(payload, repo, executor)


def evaluate_breadth_phase(payload, repo):
    """复制payload并只保留广度动作, 在绑定的只读公共仓库计算真实比例。

    不加载租户策略注册表或执行个股动作; 完整池/信号/世代仍来自本次请求。
    """
    task = copy.deepcopy(payload)
    task["strategy_task_plans"] = {sid: {**plan, "actions": [
        action for action in plan["actions"] if action["phase"] == "breadth"]}
        for sid, plan in task["strategy_task_plans"].items()}
    return evaluate_strategy_phases_serial(task, repo, None, breadth_only=True)


def evaluate_strategy_phases_serial(payload, repo, executor, *, breadth_only=False):
    """阶段顺序适配及可信广度内核; IO 消费绑定的不可变公共仓库。

    隔离运行的广度在 Broker 启动的独立任务容器使用此入口, 不加载用户源码;
    非隔离适配先广度后个股。不持久化业务结果, 不把内部候选当最终买名单。
    """
    from dataclasses import asdict

    from app.services.screener import ScreenerService
    from app.services.strategy_input_binding import frozen_context, requires_authority
    from app.services.strategy_monitor_state import finished, receipt
    from app.services.strategy_monitor_tasks import apply_independent_buy
    from app.strategy.basic_filter import previous_session
    from app.strategy.engine import StrategyDataContext
    from app.strategy.late_session_strength import Evaluation, evaluate, previous_closes

    output = {"events": [], "execution_events": [], "results": {}, "state": copy.deepcopy(payload["state"])}
    if not output["state"].get("engine"):
        output["state"]["engine"] = MonitorRuleEngine().export_state()
    states = output["state"].setdefault("engine", {}).setdefault("late_session_tasks", {})
    compute_ids = []
    joined_ids = {}
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
            reference_errors, waiting_publication, market_completed = {}, False, False
            manifest = payload.get("strategy_input_manifests", {}).get(sid, {}).get("breadth")
            try:
                symbols = pool.get("symbols")
                if symbols is None:
                    raise ValueError(pool.get("error") or "缺少该策略完整当日股票池")
                if requires_authority(payload, sid):
                    if (manifest is None or manifest["input"]["strategy_id"] != sid
                            or manifest["phase"] != "breadth"):
                        raise ValueError("权威输入未同步, 禁止使用独立行情")
                    context = frozen_context(manifest)
                    minutes, references = context.history, manifest["input"]["references"]
                    reference_errors = manifest["invalid_symbols"]
                    market_completed = True
                else:
                    release = (payload.get("snapshot") or {}).get("public_release") or {}
                    clocks = [value for key in ("minute_completed_at", "minute_priority_completed_at")
                              if isinstance(value := release.get(key), (int, float))
                              and not isinstance(value, bool) and math.isfinite(value)]
                    target = datetime.combine(now.date(), datetime.strptime(
                        window["start"], "%H:%M:%S").time(), now.tzinfo)
                    waiting_publication = bool(clocks and max(clocks) < target.timestamp())
                    market_clock = release.get("minute_completed_at")
                    market_completed = (isinstance(market_clock, (int, float))
                                        and not isinstance(market_clock, bool)
                                        and math.isfinite(market_clock)
                                        and target.timestamp() <= market_clock <= now.timestamp())
                    if not waiting_publication:
                        minutes = repo.get_minute_batch(symbols, now.date(), asset_type="stock")
                        raw_inputs = getattr(repo, "get_daily_close_inputs", None)
                        if callable(raw_inputs):
                            daily, _ = raw_inputs(previous_session(now.date()), include_closing_minute=False)
                            if "symbol" in daily.columns:
                                daily = daily.filter(pl.col("symbol").is_in(symbols))
                        else:
                            daily = ScreenerService(repo)._load_enriched_history(now.date(), 2)
                        context = StrategyDataContext("stock", "1m", now.date(),
                                                      daily_history=daily, current=pl.DataFrame())
                        references = previous_closes(context, isolate_symbols=True)
                        reference_errors = context.reference_errors
            except Exception as exc:
                error = str(exc)
            calculated = (Evaluation(stage="waiting_data", pool_total=len(symbols))
                          if waiting_publication else evaluate(
                                  minutes, sig, day=now.date(), pool_symbols=pool.get("symbols"),
                                  prev_close=references, completed_through=now, task_state=state,
                                  input_error=error, breadth_only=True,
                                  startup_publication_complete=market_completed,
                                  unavailable_symbols=manifest["invalid_symbols"] if manifest else None,
                                  source_input_sha256=manifest["input_sha256"] if manifest else None,
                                  selection=payload.get("selection_snapshots", {}).get(sid),
                                  independent_phase="breadth" if window.get("phase_policy") == "independent_v1" else None))
            completed = cn_now()
            quality = asdict(calculated)
            quality["missing_count"] = len(calculated.missing_symbols)
            quality["reference_errors"] = reference_errors
            if manifest:
                quality["input_sha256"] = manifest["input_sha256"]
                quality["input_excluded_symbols"] = manifest["invalid_symbols"]
            if waiting_publication:
                quality["reason"] = "等待启动分钟公共发布"
            old = payload["strategy_task_receipts"].get(sid) or {}
            tasks = copy.deepcopy(old.get("tasks", {}))
            task = tasks.setdefault("buy", {"signals": {}})
            saved = task.setdefault("signals", {}).get(sig["id"], {})
            phase = receipt(saved.get("breadth", {}), action, completed,
                            passed=None if waiting_publication else calculated.stage != "breadth_rejected",
                            errors=calculated.diagnostics, details=quality,
                            complete=not waiting_publication)
            signal = {**saved, "breadth": phase}
            if phase["state"] in ("rejected", "expired") and window.get("phase_policy") != "independent_v1":
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
            # 晚到广度只补展示; 保留名单不代表本次发生新买信号, 通知由首次汇合决定。
            daily_result["monitor_executed_signals"] = {"buy": [], "sell": []}
            selected = payload["selection_snapshots"].get(sid)
            daily_result.update(as_of=str(now.date()), rows=daily_result.get("rows", []),
                                total=daily_result.get("total", 0), section_mode="signals",
                                monitor_tasks=tasks, monitor_contract="strategy_monitor_v2",
                                asset_type=payload["daily_identities"][sid]["asset_type"],
                                timeframe=payload["daily_identities"][sid]["timeframe"],
                                runtime_identity=payload["daily_identities"][sid]["runtime_identity"])
            daily_result["sections"] = daily_result.get("sections") or {
                "sell": (payload["sell_rosters"].get(sid) or {}).get("rows", []),
                "select": (selected or {}).get("rows", []), "buy": []}
            daily_result["section_statuses"] = daily_result.get("section_statuses") or {}
            daily_result["section_statuses"]["buy"] = task["state"]
            daily_result["entry_data_quality"] = daily_result.get("entry_data_quality") or {}
            daily_result["entry_data_quality"][column] = quality
            daily_result["execution_status"] = {
                "state": "success" if terminal and not phase["errors"] else "failed" if phase["errors"] else "waiting",
                "reason": "上涨占比未达到阈值, 当日信号任务结束" if terminal else
                          "; ".join(phase["errors"]) if phase["errors"] else
                          "等待启动分钟公共发布" if waiting_publication else "上涨占比通过, 等待个股确认",
                "updated_at": int(completed.timestamp() * 1000)}
            daily_result = apply_independent_buy(daily_result, payload["strategy_task_windows"][sid], completed)
            tasks = daily_result["monitor_tasks"]
            prior_signals = (previous.get("monitor_tasks") or {}).get("buy", {}).get("signals", {})
            newly_joined = [key for key, value in tasks["buy"]["signals"].items()
                            if value.get("joined_at") and not prior_signals.get(key, {}).get("joined_at")]
            if not breadth_only and newly_joined and daily_result.get("entry_signal_hits"):
                joined_ids[sid] = newly_joined
                if sid not in compute_ids:
                    # 已完成名单沿原通知入口发首次汇合事件;
                    # 冻结阶段回执阻止重新取个股分钟或计算条件。
                    compute_ids.append(sid)
            output["results"][sid] = daily_result
            payload["strategy_task_receipts"][sid] = {**old, "tasks": tasks}
            payload["strategy_task_results"][sid] = daily_result
    if compute_ids:
        task_payload = {**payload, "state": output["state"], "late_task_only": True,
                        "strategy_task_ids": compute_ids,
                        "strategy_task_plans": {sid: {**payload["strategy_task_plans"][sid],
                            "actions": [a for a in payload["strategy_task_plans"][sid]["actions"]
                                        if a["phase"] != "breadth"]} for sid in compute_ids},
                        "rules": [r for r in payload["rules"] if r.get("strategy_id") in compute_ids]}
        try:
            computed = executor.execute(task_payload)
        except Exception as exc:
            # 沙箱启动/执行失败不能丢掉已完成广度或伪装零买入。
            computed = {"events": [], "execution_events": [], "state": output["state"],
                        "results": failed_phase_results(task_payload, str(exc), cn_now())}
        missing = [sid for sid in compute_ids if sid not in computed.get("results", {})]
        if missing:
            # 返回成功包但缺指定策略回执也算阶段失败, 不把缺项解释为零股票。
            computed["results"].update(failed_phase_results(
                {**task_payload, "strategy_task_ids": missing}, "计算未返回策略阶段回执", cn_now()))
        output["events"].extend(computed["events"])
        output["execution_events"].extend(computed.get("execution_events", []))
        output["results"].update(computed["results"])
        output["state"] = computed["state"]
    for sid, signals in joined_ids.items():
        daily = output["results"][sid]
        daily.setdefault("monitor_executed_signals", {})["buy"] = signals
        daily["monitor_tasks"]["buy"]["executed_signals"] = signals
    return output
