"""Explicit S05 historical task scope; never changes natural monitor clocks."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta

from app.market_time import as_cn_datetime, cn_now
from app.services.selection_clock import S05_ID


def validate_replay(receipt, target, strategy_ids):
    if (not isinstance(receipt, dict) or receipt.get("kind") != "s05_historical_tasks_v1"
            or receipt.get("strategy_id") != S05_ID or strategy_ids != [S05_ID]
            or not receipt.get("run_id")):
        raise ValueError("S05历史补算身份不匹配")
    try:
        start, end = (date.fromisoformat(receipt[key]) for key in ("start", "end"))
        requested = as_cn_datetime(datetime.fromisoformat(receipt["requested_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("S05历史补算缺少范围或实际申请时间") from exc
    target = date.fromisoformat(str(target))
    if (not start <= target <= end or requested > cn_now()
            or requested < datetime.combine(target, time(15), requested.tzinfo)):
        raise ValueError("S05历史补算只能使用范围内已收盘的交易日")
    return target


def historical_context(context, receipt, strategy_ids):
    target = validate_replay(receipt, context.as_of, strategy_ids)
    close = datetime.combine(target, time(15))
    return replace(context, realtime=False, observed_at=close, completed_through=close,
                   signal_task_states=None)


def replay_entries(engine, strategy, context, overrides):
    """Run each saved template independently; one bad input cannot cancel another."""
    from app.strategy.engine import StrategyEngine

    results = {}
    for column in engine._effective_signals(overrides, "entry_signals", strategy.entry_signals):
        effective = {**deepcopy(overrides), "entry_signals": [column], "exit_signals": []}
        try:
            # This explicitly requested audit persists per-branch evidence. The
            # caller publishes only daily_entries, never this unscoped comparison.
            result = asdict(StrategyEngine.run(engine, S05_ID, context, overrides=effective))
        except Exception as exc:
            result = {"as_of": str(context.as_of), "strategy_id": S05_ID, "rows": [], "total": 0,
                      "section_statuses": {"buy": "failed"}, "section_errors": {"buy": str(exc)},
                      "entry_filter_error": str(exc), "entry_signal_hits": []}
        results[column.removeprefix("csgi_")] = result
    return results


def daily_entries(entries, selection, day):
    """All requested branches remain in the audit; the daily view uses its frozen branch."""
    from app.strategy.s02_173004552 import ENTRY_IDS, frozen_daily_branch
    wanted = ENTRY_IDS.get(frozen_daily_branch(selection, day))
    return {wanted: entries[wanted]} if wanted else {}


def replay_strategy(engine, service, context, payload):
    """Return historical entries and paired notification exits, without trading."""
    from app.services.strategy_monitor_tasks import evaluate_sell_task, sell_context, task_windows

    strategy = engine.get(S05_ID)
    overrides = payload.get("overrides_map", {}).get(S05_ID, {})
    selected = (context.selection_snapshots or {}).get(S05_ID, {})
    all_entries = replay_entries(engine, strategy, context, overrides)
    entries = daily_entries(all_entries, selected, context.as_of)
    rows, hits, quality, buy_errors = {}, {}, {}, []
    for signal, result in entries.items():
        error = (result.get("section_errors") or {}).get("buy") or result.get("entry_filter_error")
        if error:
            buy_errors.append(f"{signal}: {error}")
        for row in result.get("rows") or []:
            rows.setdefault(row["symbol"], row)
        for hit in result.get("entry_signal_hits") or []:
            key = hit["symbol"]
            if key in hits:
                hits[key]["signals"] = sorted(set(hits[key]["signals"]) | set(hit["signals"]))
            else:
                hits[key] = deepcopy(hit)
        quality.update(result.get("entry_data_quality") or {})
    exits, exit_hits, sell_errors = {}, [], []
    windows = task_windows(engine, strategy, overrides)
    rosters = payload.get("prior_branch_rosters") or {}
    for window in windows["sell"]:
        signal = window["signal"]["id"]
        paired = signal.replace("_exit_", "_entry_")
        roster = rosters.get(paired)
        observed = datetime.combine(context.as_of, time.fromisoformat(window["end"])) - timedelta(
            microseconds=1)
        scoped_windows = {**windows, "sell": [window]}
        scoped = sell_context(service, context.as_of, context.current, roster, observed)
        evaluated = evaluate_sell_task(engine, strategy, scoped, overrides, scoped_windows, roster)
        exits[signal] = evaluated
        exit_hits.extend(evaluated["hits"])
        if evaluated["state"] not in ("success", "rejected"):
            sell_errors.extend(evaluated["errors"] or ["缺少前交易日买入归档, 卖出名单待确认"])
    ordered = sorted(rows.values(), key=lambda row: (
        -(row.get("score") if isinstance(row.get("score"), (int, float)) else float("-inf")),
        row["symbol"]))
    selected_exit_ids = {signal.replace("_entry_", "_exit_") for signal in entries}
    daily_exits = {key: value for key, value in exits.items() if key in selected_exit_ids}
    exit_hits = [hit for value in daily_exits.values() for hit in value["hits"]]
    sell_errors = [error for value in daily_exits.values() for error in value["errors"]]
    statuses = {"select": "success" if selected else "waiting",
                "buy": "failed" if buy_errors else "success",
                "sell": "waiting" if sell_errors else "success"}
    errors = {}
    if buy_errors:
        errors["buy"] = "; ".join(dict.fromkeys(buy_errors))
    if sell_errors:
        errors["sell"] = "; ".join(dict.fromkeys(sell_errors))
    completed = cn_now().isoformat()

    def task(values, kind):
        signals = {}
        for signal, result in values.items():
            error = ((result.get("section_errors") or {}).get("buy") or result.get("entry_filter_error")
                     if kind == "buy" else "; ".join(result["errors"]) or None)
            normal = ((result.get("section_statuses") or {}).get("buy") == "success"
                      if kind == "buy" else result["state"] == "success")
            count = len(result.get("rows") or []) if kind == "buy" else len(result["hits"])
            signals[signal] = {
                "state": "success" if normal and count else "rejected" if normal else "failed",
                "status": "succeeded" if normal else "failed", "terminal": True,
                "passed": bool(count) if normal else None, "count": count,
                "errors": [error] if error else [], "completed_at": completed,
                "manual_replay": payload["historical_replay"]}
        return {"state": statuses[kind], "status": "succeeded" if statuses[kind] == "success" else "failed",
                "terminal": True, "published": statuses[kind] == "success",
                "signals": signals, "completed_at": completed,
                "errors": [errors[kind]] if kind in errors else [],
                "count": len(ordered) if kind == "buy" else len(exit_hits)}

    return {"results": {S05_ID: {
        "as_of": str(context.as_of), "strategy_id": S05_ID, "rows": ordered, "total": len(ordered),
        "section_mode": "signals", "selection_snapshot": selected,
        "sections": {"sell": [], "select": selected.get("rows", []), "buy": ordered},
        "section_statuses": statuses, "section_errors": errors,
        "entry_filter_error": errors.get("buy"), "entry_signal_hits": list(hits.values()),
        "exit_signal_hits": exit_hits, "entry_data_quality": quality,
        "daily_stock_pool": (context.daily_pool_snapshots or {}).get(S05_ID),
        "manual_replay": {**payload["historical_replay"], "computed_at": completed},
        "monitor_tasks": {"buy": task(entries, "buy"), "sell": task(daily_exits, "sell")},
        "branch_code": selected["branch_code"],
        "historical_branch_tasks": {"buy": task(all_entries, "buy"), "sell": task(exits, "sell")},
        "replay_branches": {"entry": all_entries, "exit": exits},
    }}}
