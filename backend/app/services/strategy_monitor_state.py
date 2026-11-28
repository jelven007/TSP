"""按交易日规划卖/选/买阶段并汇合回执; 行情更新不重开已完成阶段。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta

from app.market_time import as_cn_datetime

CONTRACT = "strategy_monitor_v2"
TERMINAL = frozenset({"success", "rejected", "expired", "cancelled"})


def finished(receipt):
    """正常通过、拒绝、取消或过期结束阶段; 可修复的failed仍可重试。"""
    return receipt.get("terminal") is True or receipt.get("state") in TERMINAL


def _at(now, clock):
    return datetime.combine(now.date(), time.fromisoformat(clock), now.tzinfo)


def resume_confirmation_timeout(windows, tasks, now):
    """只恢复同窗口内旧提前确认超时; 已发布买名单和正常终态不受影响。"""
    now = as_cn_datetime(now)
    tasks = deepcopy(tasks)
    buy = tasks.get("buy") or {}
    if buy.get("state") != "expired":
        return tasks
    for window in windows.get("buy") or []:
        params = window["signal"].get("params") or {}
        confirm = _at(now, window["start"]) + timedelta(
            minutes=float(params.get("evaluation_delay_minutes") or 0))
        end = min(_at(now, window["end"]), _at(now, "14:55:00"))
        signal = (buy.get("signals") or {}).get(window["signal"]["id"]) or {}
        breadth = signal.get("breadth") or {}
        if (window["signal"].get("template") != "late_session_strength"
                or params.get("pool_up_min_pct") is None or not confirm <= now < end
                or breadth.get("state") != "expired"
                or breadth.get("retry_until") != confirm.isoformat()
                or (signal.get("stocks") or {}).get("state") in ("success", "rejected")
                or buy.get("published")):
            continue
        resumed = {"state": "failed", "status": "queued", "terminal": False,
                   "passed": None, "completed_at": None, "next_at": now.isoformat(),
                   "retry_until": end.isoformat(), "resumed_at": now.isoformat(),
                   "resume_reason": "确认时间为最早计算时间, 输入在买入窗口内继续修复"}
        breadth.update(resumed)
        breadth.pop("repair", None)
        signal.update(resumed)
        buy.update(resumed)
    return tasks


def plan(windows, tasks, now, *, trading, include_select=False, retry_seconds=3, strategy_id=None):
    """只规划未完成阶段; 未到确认等待, 失败按next_at重试同份启动输入。

    breadth期限不含结束整秒, stocks允许结束整秒; 已完成买仍可补空下限广度观测。
    S03的10:30为修复/提交截止, 不是逐分钟重新选股的区间。
    当前S03没有独立阶段/广度观测声明且广度下限为空, 直接规划确认时刻的
    stocks。返回actions及next_at, 不执行计算、补数或写入回执; 交易日未知不派发。
    """
    now = as_cn_datetime(now)
    actions, wakes = [], []
    if trading is not True:
        return {"actions": actions, "next_at": None}

    def due(kind, signal, phase, start, deadline, receipt, *, strict=False, observation_only=False):
        if finished(receipt):
            return
        if now > deadline or (strict and now >= deadline):
            actions.append({"kind": kind, "signal_id": signal, "phase": "expire",
                            "expired_phase": phase, "scheduled_at": start.isoformat(),
                            "retry_until": deadline.isoformat(),
                            **({"observation_only": True} if observation_only else {})})
            return
        retry = receipt.get("next_at")
        ready = max(start, as_cn_datetime(datetime.fromisoformat(retry))) if retry else start
        if now < ready:
            wakes.append(ready)
            return
        actions.append({"kind": kind, "signal_id": signal, "phase": phase,
                        "scheduled_at": start.isoformat(), "retry_until": deadline.isoformat(),
                        **({"observation_only": True} if observation_only else {})})
        wakes.append(min(now + timedelta(seconds=30 if phase == "observe" else retry_seconds), deadline))

    buy = tasks.get("buy") or {}
    for window in windows.get("buy") or []:
        if finished(buy) and window.get("observe_breadth") is not True:
            continue
        sig = window["signal"]
        signal = sig["id"]
        saved = buy.get("signals", {}).get(signal, {})
        start = _at(now, window["start"])
        end = min(_at(now, window["end"]), _at(now, "14:55:00"))
        params = sig.get("params") or {}
        confirm = start + timedelta(minutes=float(params.get("evaluation_delay_minutes") or 0))
        breadth = (sig.get("template") == "late_session_strength"
                   and params.get("pool_up_min_pct") is not None)
        observation = (sig.get("template") == "late_session_strength"
                       and window.get("observe_breadth") is True
                       and window.get("phase_policy") == "independent_v1" and not breadth)
        if (finished(buy) or finished(saved)) and not observation:
            continue
        if sig.get("template") == "s01_late_entry" and now < start < confirm:
            # Frozen native signals retain their original start pulse.
            wakes.append(start)
        # 空下限观测也用完整池, 但不作为买入必需阶段; 其截止不包含结束整秒。
        if breadth or observation:
            due("buy", signal, "breadth", start + timedelta(seconds=15), end,
                saved.get("breadth") or {}, strict=True, observation_only=observation)
        if finished(buy) or finished(saved):
            continue
        if window.get("phase_policy") == "independent_v1" or not breadth or finished(
                saved.get("breadth") or {}):
            # 独立策略不等待广度成功; stocks内三项子计算由一次动作顺序完成。
            due("buy", signal, "stocks", confirm, end, saved.get("stocks") or {})

    sell = tasks.get("sell") or {}
    if not finished(sell) or (windows.get("sell") and sell.get("completion_reason") == "s05_no_daily_branch"):
        for window in windows.get("sell") or []:
            signal = window["signal"]["id"]
            due("sell", signal, "observe", _at(now, window["start"]), _at(now, window["end"]),
                sell.get("signals", {}).get(signal, {}), strict=True)
    if include_select:
        from app.services.selection_clock import window_text
        start, end = window_text(strategy_id)
        due("select", "", "select", _at(now, start), _at(now, end),
            tasks.get("select") or {})
    return {"actions": actions, "next_at": min(wakes).isoformat() if wakes else None}


def independent_buy(windows):
    """阶段策略来自保存源码声明, 不用策略名称或固定ID猜测执行能力。"""
    return any(w.get("phase_policy") == "independent_v1" for w in windows)


def join_buy(task, windows, now):
    """按必需阶段汇合: 正常通过发布候选, 正常拒绝发布零, 失败/未知不发布。

    空广度下限只依赖stocks; 有下限时两侧均须正常终结, 单侧拒绝不能掩盖另一侧缺输入。
    晚到观测保留原joined_at, 不能形成重复入场。
    返回新任务和最终 rows/hits, 不修改输入 task; 实际持久化仍由 Worker
    在定义、世代、租约和提交时钟守卫通过后执行。
    """
    task = deepcopy(task)
    now = as_cn_datetime(now)
    signals = task.setdefault("signals", {})
    rows, hits, published = {}, {}, False
    for window in windows:
        value = signals.setdefault(window["signal"]["id"], {})
        stocks = value.get("stocks") or {}
        if window.get("phase_policy") == "independent_v1":
            value["phase_policy"] = "independent_v1"
            value["breadth_required"] = (window["signal"].get("params") or {}).get("pool_up_min_pct") is not None
            breadth = value.get("breadth") or {}
            # 空广度下限仅展示, 个股回执独立决定终态; 有下限才等待两侧正常完成。
            phases = [stocks]
            if value["breadth_required"]:
                phases.append(breadth)
            # 已完成汇合保持原完成时钟, 迟到观测只能补展示, 不能改写已发布买。
            if not finished(value):
                terminal = all(finished(p) for p in phases)
                normal = terminal and all(p.get("state") in ("success", "rejected") for p in phases)
                errors = list(dict.fromkeys(e for p in phases for e in p.get("errors", [])))
                end = min(_at(now, window["end"]), _at(now, "14:55:00"))
                late = normal and now > end
                states = {p.get("state") for p in phases}
                passed = normal and not late and all(p.get("passed") is True for p in phases)
                state = ("expired" if late or (terminal and "expired" in states) else
                         "cancelled" if terminal and "cancelled" in states else
                         "success" if passed else "rejected" if normal else
                         "failed" if errors or "failed" in states else "waiting")
                if late:
                    errors.append("两类条件汇合时已超过实时入场截止")
                value.update(state=state, terminal=terminal, passed=passed if terminal else None,
                             status="succeeded" if normal and not late else
                             "failed" if terminal else "queued", errors=errors,
                             completed_at=now.isoformat() if terminal else None)
                if normal and not late:
                    value["joined_at"] = now.isoformat()
            public = value.get("state") in ("success", "rejected") and finished(value)
            eligible = public and value.get("passed") is True
        else:
            public = stocks.get("state") in ("success", "rejected")
            eligible = public
        published |= public
        if not eligible:
            continue
        for row in stocks.get("rows") or []:
            row = deepcopy(row)
            if value.get("joined_at"):
                row["entry_evaluated_at"] = as_cn_datetime(datetime.fromisoformat(
                    value["joined_at"])).replace(tzinfo=None).isoformat()
            rows.setdefault(row["symbol"], row)
        for hit in stocks.get("hits") or []:
            hit = deepcopy(hit)
            if value.get("joined_at"):
                hit["entry_evaluated_at"] = as_cn_datetime(datetime.fromisoformat(
                    value["joined_at"])).replace(tzinfo=None).isoformat()
            symbol = hit["symbol"]
            if symbol in hits:
                hits[symbol]["signals"] = sorted(set(hits[symbol].get("signals", []))
                                                | set(hit.get("signals", [])))
            else:
                hits[symbol] = hit
    values = [signals[w["signal"]["id"]] for w in windows]
    terminal = bool(values and all(finished(s) for s in values))
    states = {s.get("state") for s in values}
    state = ("unconfigured" if not values else
             "expired" if terminal and "expired" in states else
             "cancelled" if terminal and "cancelled" in states else
             "success" if terminal and "success" in states else
             "rejected" if terminal else "failed" if "failed" in states else "waiting")
    task.update(state=state, terminal=terminal, published=published, count=len(rows),
                status="succeeded" if terminal and state in ("success", "rejected") else
                "failed" if terminal else "queued",
                errors=list(dict.fromkeys(e for s in values for e in s.get("errors", []))))
    if terminal:
        task["completed_at"] = max(s.get("completed_at") or now.isoformat() for s in values)
    return task, sorted(rows.values(), key=lambda r: (
        -(r.get("score") if isinstance(r.get("score"), (int, float)) else float("-inf")),
        r["symbol"])), list(hits.values())


def receipt(previous, action, now, *, passed=None, errors=(), details=None, complete=True):
    """保存首错、最近32次尝试和阶段终态; 正常零结果立即固定。

    输入失败按3秒计划重试, 不承诺实际执行耗时; 广度/卖期限不含结束整秒。
    返回深拷贝结果; details承载内核实际计数, 本层不推算缺失计数。
    complete=False保留未终结卖观察, 已有首次命中由任务层独立累积;
    只有无错误且complete才正常冻结。此回执不是成交确认或归档提交凭据。
    """
    if finished(previous):
        return deepcopy(previous)
    now = as_cn_datetime(now)
    errors = list(errors)
    deadline = as_cn_datetime(datetime.fromisoformat(action["retry_until"]))
    expired = action["phase"] == "expire" or now > deadline or (
        (action["phase"] == "breadth" or action.get("kind") == "sell") and now >= deadline)
    terminal = expired or (not errors and complete)
    state = ("expired" if expired else "failed" if errors else
             "rejected" if passed is False and complete else "success" if complete else "running")
    attempt = {
        "started_at": now.isoformat(), "state": state, "errors": errors,
        "details": deepcopy(details or {}),
    }
    attempts = previous.get("attempts", 0) + 1
    return {
        **deepcopy(previous), **deepcopy(details or {}), "state": state,
        **({"observation_only": True} if action.get("observation_only") else {}),
        "status": ("failed" if expired else "succeeded" if terminal else "queued"),
        "terminal": terminal, "passed": passed, "phase": action.get("expired_phase", action["phase"]),
        "scheduled_at": action["scheduled_at"], "retry_until": action["retry_until"],
        "started_at": previous.get("started_at", now.isoformat()),
        "completed_at": now.isoformat() if terminal else None, "attempts": attempts,
        "first_error": previous.get("first_error") or (errors[0] if errors else None),
        "errors": errors,
        "first_receipt": deepcopy(previous.get("first_receipt")
            or next(iter(previous.get("history", [])), attempt)),
        "history": [*previous.get("history", [])[-31:], attempt],
        "next_at": None if terminal else min(now + timedelta(
            seconds=30 if action["phase"] == "observe" and not errors else 3), deadline).isoformat(),
    }
