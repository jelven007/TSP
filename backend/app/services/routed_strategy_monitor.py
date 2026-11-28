"""Plan only the independently saved strategy's frozen entry and exit branch."""
from __future__ import annotations

from app.services import strategy_monitor_tasks as base
from app.strategy import s02_173004552 as routed


def _common_windows(engine, strategy, overrides, *, day, selection, roster):
    effective = {**overrides, "entry_signals": []}
    metadata = {}
    try:
        branch = routed.frozen_daily_branch(selection, day)
    except ValueError as exc:
        metadata["selection_error"] = str(exc)
    else:
        metadata.update(branch_code=selection["branch_code"], branch_source_date=selection["source_date"])
        if branch is None:
            metadata["buy_no_market_route"] = routed.no_market_route_receipt(selection)
        else:
            wanted = "csgi_" + routed.ENTRY_IDS[branch]
            effective["entry_signals"] = [name for name in engine._effective_signals(
                overrides, "entry_signals", strategy.entry_signals) if name == wanted]
    windows = base.task_windows(engine, strategy, effective)
    windows.update(metadata)
    from app.strategy.basic_filter import previous_session

    if roster is None or roster.get("source_date") != str(previous_session(day)):
        return {**windows, "sell": [], "sell_source_error": "缺少前一交易日买入归档, 卖名单待确认"}
    by_id = {w["signal"]["id"]: w for w in windows["sell"]}
    sell, missing = {}, []
    for row in roster["rows"]:
        exit_id = routed.EXIT_IDS.get(row.get("entry_branch"))
        if exit_id in by_id:
            sell.setdefault(exit_id, []).append(row["symbol"])
        else:
            missing.append(row["symbol"])
    windows["sell"] = [{**by_id[sid], "symbols": sorted(set(symbols))} for sid, symbols in sell.items()]
    if missing:
        windows["sell_source_error"] = "缺少原买入分支或对应启用的出场信号: " + "、".join(sorted(set(missing)))
    elif not roster["rows"]:
        windows["sell_no_market_route"] = {
            "state": "success", "status": "succeeded", "terminal": True, "executed": False,
            "windows": [], "errors": [], "count": 0, "completion_reason": "empty_archived_sell_roster",
            "source_date": roster["source_date"]}
    return windows


def task_windows(engine, strategy, overrides, *, day=None, selection=None, roster=None,
                 require_market_route=False):
    if strategy.meta.get("id") != routed.SID or day is None:
        return base.task_windows(engine, strategy, overrides)
    if not routed.enabled(strategy):
        return _common_windows(engine, strategy, overrides, day=day, selection=selection, roster=roster)
    strict = require_market_route or not routed.enabled(strategy)
    try:
        branch = (routed.frozen_daily_branch(selection, day) if strict
                  else routed.frozen_route(selection, day))
    except ValueError as exc:
        if strict:
            return {"buy": [], "sell": [], "selection_error": str(exc), "sell_source_error": str(exc)}
        windows = base.task_windows(engine, strategy, overrides)
        windows.update(buy=[], selection_error=str(exc))
    else:
        numeric = (selection.get("entry_route") or {}).get("schema") == routed.MARKET_ROUTE
        effective = dict(overrides)
        if numeric:
            for key, ids in (("entry_signals", routed.ENTRY_IDS), ("exit_signals", routed.EXIT_IDS)):
                selected = engine._effective_signals(overrides, key, getattr(strategy, key))
                wanted = "csgi_" + ids[branch] if branch is not None else None
                effective[key] = [name for name in selected if name == wanted]
        windows = base.task_windows(engine, strategy, effective)
        if numeric:
            windows["branch_code"] = selection["branch_code"]
            windows["branch_source_date"] = selection["source_date"]
        if branch is None:
            windows.update(buy=[], buy_no_market_route=routed.no_market_route_receipt(selection))
            if numeric:
                windows.update(sell=[], sell_no_market_route={
                    **routed.no_market_route_receipt(selection),
                    "completion_reason": "s05_no_daily_branch", "reason": "分支参数4, 不安排买卖定时任务"})
                return windows
        else:
            windows["buy"] = [window for window in windows["buy"]
                              if window["signal"]["id"] == routed.ENTRY_IDS[branch]]
            if numeric:
                windows["sell"] = [window for window in windows["sell"]
                                   if window["signal"]["id"] == routed.EXIT_IDS[branch]]
            if routed.enabled(strategy):
                for window in windows["buy"]:
                    routed.branch_clock(window["signal"], day)
        if numeric:
            if roster is None:
                windows["sell_source_error"] = "缺少前一交易日买入归档, 卖名单待确认"
            return windows
    if roster is None:
        windows.update(sell=[], sell_source_error="缺少前一交易日买入归档, 卖名单待确认")
    elif roster["rows"]:
        ids = {routed.EXIT_IDS.get(row.get("entry_branch")) for row in roster["rows"]}
        windows["sell"] = [window for window in windows["sell"]
                           if window["signal"]["id"] in ids]
    return windows


def strategy_windows(engine, strategy, storage, overrides, *, day):
    if strategy.meta.get("id") != routed.SID or callable(getattr(engine, "_monitor_task_loader", None)):
        return base.strategy_windows(engine, strategy, storage, overrides)
    if not base.monitoring_state(storage, routed.SID).get("enabled"):
        return {"buy": [], "sell": []}
    from app.market_time import cn_today
    from app.services.daily_selections import DailySelections

    return task_windows(engine, strategy, overrides, day=day,
                        selection=DailySelections(storage).read(routed.SID, day, live_entry=day == cn_today()),
                        roster=base.sell_roster(storage, routed.SID, day), require_market_route=True)


def monitoring_summary(engine, storage, *, now=None, strategy_id=None):
    from app.market_time import as_cn_datetime, cn_now
    from app.strategy.config import load_override

    now = as_cn_datetime(now or cn_now())
    value = base.monitoring_summary(engine, storage, now=now, strategy_id=strategy_id)
    if not engine.has(routed.SID):
        return value
    row = next((r for r in value["strategies"] if r["strategy_id"] == routed.SID), None)
    if not row or not row["enabled"]:
        return value
    windows = strategy_windows(engine, engine.get(routed.SID), storage, load_override(storage, routed.SID), day=now.date())
    if not windows:
        return value
    for field in ("branch_code", "branch_source_date"):
        if field in windows:
            row[field] = windows[field]
    for kind in ("buy", "sell"):
        task = row["tasks"][kind]
        if kind == "sell" and windows["sell"] and task.get("completion_reason") == "s05_no_daily_branch":
            task = row["tasks"][kind] = {"errors": [], "count": 0}
        task["windows"] = [{k: window[k] for k in ("start", "end")} | {
            "signal_id": window["signal"]["id"], "signal_name": window["signal"]["name"]}
            for window in windows[kind]]
        if not base.finished(task):
            task["state"] = base.window_state(windows, kind, now)
    if windows.get("selection_error"):
        row["tasks"]["buy"].update(state="blocked", errors=[windows["selection_error"]])
    elif windows.get("buy_no_market_route") and not row["tasks"]["buy"].get("count"):
        row["tasks"]["buy"] = windows["buy_no_market_route"]
    if windows.get("sell_no_market_route") and not row["tasks"]["sell"].get("count"):
        row["tasks"]["sell"] = windows["sell_no_market_route"]
    if windows.get("sell_source_error") and not base.finished(row["tasks"]["sell"]):
        row["tasks"]["sell"].update(state="waiting", errors=[windows["sell_source_error"]])
    return value


def s05_monitoring_day(engine, storage, day, *, now=None):
    """Project saved branch clocks and execution evidence; never schedule or replay."""
    import gzip
    import json

    from app.market_time import as_cn_datetime, cn_now, published_trading_day
    from app.services.daily_selections import DailySelections, selection_record
    from app.services.strategy_results import StrategyResults
    from app.strategy.basic_filter import previous_session
    from app.strategy.config import load_override

    now = as_cn_datetime(now or cn_now())
    sid = routed.SID
    trading = published_trading_day(day)
    historical = day < now.date()
    states = {False: "non_trading_day", None: "calendar_unknown"}
    default_state = states.get(trading, "no_receipt" if historical else "waiting")
    receipts = base.task_receipt(storage, sid, day).get("tasks", {})
    result = {}
    store = StrategyResults(storage)
    if historical and trading is True and (view := store.day_view(sid, day)):
        result = json.loads(gzip.decompress(view[1]))["result"]

    enabled = sid in base.monitored_ids(storage)
    live = {}
    if day == now.date() and trading is True:
        live = monitoring_summary(engine, storage, now=now, strategy_id=sid)["strategies"][0]["tasks"]

    def archived_task(kind):
        actual = receipts.get(kind, {})
        value = actual or live.get(kind) or {"state": default_state}
        task = {**value, "windows": []}
        if actual.get("state"):
            task["receipt_source"] = "scheduler"
            task["receipt_windows"] = actual.get("windows") or []
        return task

    select = archived_task("select")
    select["windows"] = select.pop("receipt_windows", []) or live.get("select", {}).get("windows", [])
    tasks = {"select": select, "buy": archived_task("buy"), "sell": archived_task("sell")}
    row = {"strategy_id": sid, "enabled": enabled, "as_of": str(day), "historical": historical,
           "is_trading_day": trading, "tasks": tasks}
    if trading is not True:
        return row

    try:
        prior = previous_session(day)
    except ValueError as exc:
        prior = None
        tasks["sell"]["plan_issue"] = str(exc)
    strategy = engine.get(sid)
    overrides = load_override(storage, sid)
    for kind, branch_day, key, ids in (
        ("buy", day, "entry_signals", routed.ENTRY_IDS),
        ("sell", prior, "exit_signals", routed.EXIT_IDS),
    ):
        if branch_day is None:
            continue
        task = tasks[kind]
        task["branch_date"] = str(branch_day)
        if kind == "buy" and day == now.date():
            selection = DailySelections(storage).read(sid, branch_day, live_entry=True)
        else:
            saved = selection_record(sid, branch_day, lambda record_id: store.detail(sid, record_id))
            selection = (saved or {}).get("result", {}).get("selection_snapshot")
        try:
            branch = routed.frozen_daily_branch(selection, branch_day)
        except ValueError as exc:
            task["plan_issue"] = str(exc)
            continue
        task["branch_code"] = selection["branch_code"]
        if branch is None:
            continue

        signal_id = ids[branch]
        column = "csgi_" + signal_id
        chosen = [name for name in engine._effective_signals(overrides, key, getattr(strategy, key))
                  if name == column]
        try:
            windows = base.task_windows(engine, strategy,
                                        {**overrides, "entry_signals": [], "exit_signals": [], key: chosen},
                                        apply_entry_cutoff=False)[kind]
        except ValueError as exc:
            task["plan_issue"] = str(exc)
        else:
            task["windows"] = [
                {"start": w["start"], "end": w["end"], "signal_id": signal_id, "signal_name": w["signal"]["name"]}
                for w in windows
            ]
            if not windows:
                task["plan_issue"] = "对应分支信号未启用或缺少起止时间"

        archived = (result.get("monitor_tasks") or {}).get(kind) or {}
        if manual := result.get("manual_replay"):
            # Older daily replays selected sell by T, but saved the per-branch results too.
            branch_task = (result.get("historical_branch_tasks") or {}).get(kind) or {}
            archived = (branch_task.get("signals") or {}).get(signal_id) or (
                archived.get("signals") or {}).get(signal_id) or {}
        if archived.get("state"):
            task["archived_result"] = {
                **{k: archived[k] for k in ("state", "count", "completed_at", "errors") if k in archived},
                "source": "replay" if manual else "archive",
            }
    return row
