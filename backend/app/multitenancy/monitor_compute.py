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
            errors = task.get("errors") or []
            if not errors or task.get("state") not in ("failed", "expired", "waiting"):
                continue
            key = f"{sid}:task:{kind}"
            signature = hashlib.sha256(json.dumps(
                [daily.get("as_of"), daily.get("runtime_identity"), kind, errors],
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if state.get(key, {}).get("signature") == signature:
                continue
            state[key] = {"as_of": str(now.date()), "signature": signature}
            label = {"buy": "买", "sell": "卖", "select": "选"}.get(kind, kind)
            events.append({
                "id": hashlib.sha256(f"task-failed:{rule['id']}:{signature}".encode()).hexdigest(),
                "ts": int(now.timestamp() * 1000), "source": "system",
                "type": "signal_calculation_failed", "strategy_id": sid,
                "rule_id": rule["id"], "rule_name": rule.get("name", ""),
                "name": label, "symbol": "", "severity": "warning",
                "message": f"{rule.get('name', sid)} · {daily.get('as_of')} · {label}任务: "
                           f"{errors[0]}", "signals": [kind], "price": None, "change_pct": None,
            })
        for signal, quality in (daily.get("entry_data_quality") or {}).items():
            progress = quality.get("progress") or {}
            failed = [(step, value.get("reason")) for step, value in progress.get("steps", {}).items()
                      if value.get("state") == "failed"]
            if not failed and progress.get("failure_reason"):
                failed = [(progress.get("phase", "stocks"), progress["failure_reason"])]
            if not failed:
                continue
            phase = progress.get("phase") or ("breadth" if failed[0][0] == "breadth" else "stocks")
            key = f"{sid}:{signal}:{phase}"
            signature = hashlib.sha256(json.dumps(
                [daily.get("as_of"), daily.get("runtime_identity"), progress.get("params"), failed],
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
                           f"{failed[0][1]}; 在有效期限内持续重试",
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
                prev_close={}, completed_through=now, task_state=state, input_error=reason)
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
