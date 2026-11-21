"""Pure planning of dated monitor phases. Quotes never reopen a completed phase."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta

from app.market_time import as_cn_datetime

CONTRACT = "strategy_monitor_v2"
TERMINAL = frozenset({"success", "rejected", "expired", "cancelled"})


def finished(receipt):
    return receipt.get("terminal") is True or receipt.get("state") in TERMINAL


def _at(now, clock):
    return datetime.combine(now.date(), time.fromisoformat(clock), now.tzinfo)


def plan(windows, tasks, now, *, trading, include_select=False, retry_seconds=3, strategy_id=None):
    now = as_cn_datetime(now)
    actions, wakes = [], []
    if trading is not True:
        return {"actions": actions, "next_at": None}

    def due(kind, signal, phase, start, deadline, receipt, *, strict=False):
        if finished(receipt):
            return
        if now > deadline or (strict and now >= deadline):
            actions.append({"kind": kind, "signal_id": signal, "phase": "expire",
                            "expired_phase": phase, "scheduled_at": start.isoformat(),
                            "retry_until": deadline.isoformat()})
            return
        retry = receipt.get("next_at")
        ready = max(start, as_cn_datetime(datetime.fromisoformat(retry))) if retry else start
        if now < ready:
            wakes.append(ready)
            return
        actions.append({"kind": kind, "signal_id": signal, "phase": phase,
                        "scheduled_at": start.isoformat(), "retry_until": deadline.isoformat()})
        wakes.append(min(now + timedelta(seconds=30 if phase == "observe" else retry_seconds), deadline))

    buy = tasks.get("buy") or {}
    if not finished(buy):
        for window in windows.get("buy") or []:
            sig = window["signal"]
            signal = sig["id"]
            saved = buy.get("signals", {}).get(signal, {})
            if finished(saved):
                continue
            start = _at(now, window["start"])
            end = min(_at(now, window["end"]), _at(now, "14:55:00"))
            params = sig.get("params") or {}
            confirm = start + timedelta(minutes=float(params.get("evaluation_delay_minutes") or 0))
            breadth = (sig.get("template") == "late_session_strength"
                       and params.get("pool_up_min_pct") is not None)
            if breadth and not finished(saved.get("breadth") or {}):
                due("buy", signal, "breadth", start + timedelta(seconds=15), min(confirm, end),
                    saved.get("breadth") or {}, strict=True)
            else:
                due("buy", signal, "stocks", confirm, end, saved.get("stocks") or {})

    sell = tasks.get("sell") or {}
    if not finished(sell):
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


def receipt(previous, action, now, *, passed=None, errors=(), details=None, complete=True):
    """A failure remains retryable only within this exact phase's deadline."""
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
