"""Failure metadata is separate from the last successful stock snapshot."""
from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime


def status(state: str, reason: str | None = None, *, sequence=None, fingerprint=None) -> dict:
    return {"state": state, "reason": reason, "updated_at": time.time_ns() // 1_000_000,
            "sequence": sequence, "fingerprint": fingerprint}


def result_status(result: dict) -> dict:
    if result.get("execution_status"):
        return result["execution_status"]
    if result.get("selection_only"):
        return status("waiting", "仅预选, 买入信号尚未计算")
    sections = result.get("section_statuses") or {}
    if sections.get("buy") == "expired":
        return status("expired", (result.get("section_errors") or {}).get("buy"))
    if sections.get("buy") == "failed" or result.get("execution_error"):
        return status("failed", result.get("entry_filter_error") or result.get("execution_error")
                      or (result.get("section_errors") or {}).get("buy"))
    if result.get("entry_filter_error") or (result.get("section_errors") or {}).get("buy"):
        return status("waiting", result.get("entry_filter_error")
                      or result["section_errors"]["buy"])
    return status("success")


def merge_result(previous: dict | None, current: dict) -> dict:
    """Retain rows only for the same session and definition, visibly marked stale."""
    output = deepcopy(current)
    state = output.get("execution_status") or result_status(output)
    output["execution_status"] = state
    if state["state"] == "success":
        output["last_success_at"] = output.get("computed_at")
        return output
    if (previous and previous.get("as_of") == output.get("as_of")
            and previous.get("runtime_identity") == output.get("runtime_identity")
            and previous.get("last_success_at", previous.get("computed_at")) is not None):
        if output.get("sections") is None:
            output = {**deepcopy(previous), **output}
            output["execution_status"] = state
        output["rows"] = deepcopy(previous.get("rows") or [])
        output["total"] = previous.get("total", 0)
        output["computed_at"] = previous.get("computed_at")
        output["last_success_at"] = previous.get("last_success_at", previous.get("computed_at"))
        if output.get("sections") is not None and previous.get("sections"):
            output["sections"]["buy"] = deepcopy(previous["sections"].get("buy") or [])
        output["showing_last_success"] = True
    return output


def entry_events_before_deadline(events: list[dict], results: dict, now: datetime) -> list[dict]:
    from app.market_time import CN_TZ

    observed = now.astimezone(CN_TZ).replace(tzinfo=None) if now.tzinfo else now
    output = []
    for event in events:
        deadline = (results.get(event.get("strategy_id")) or {}).get("entry_deadline_at")
        if event.get("type") in ("buy_signal", "pool_entry") and deadline:
            try:
                end = datetime.fromisoformat(deadline)
                end = end.astimezone(CN_TZ).replace(tzinfo=None) if end.tzinfo else end
                if observed > end:
                    continue
            except (TypeError, ValueError):
                continue
        output.append(event)
    return output


def expire_late_entries(result: dict, deadlines: dict, now: datetime) -> None:
    """Fence all new entry outputs before any archive, alert or order is written."""
    from app.market_time import as_cn_datetime

    observed = as_cn_datetime(now)
    guards = {sid: {"entry_deadline_at": end} for sid, end in deadlines.items()}
    for sid, end in deadlines.items():
        daily = result["results"].get(sid)
        if daily is None:
            continue
        daily["entry_deadline_at"] = end
        if observed <= as_cn_datetime(datetime.fromisoformat(end)):
            continue
        reason = "计算结果提交时已超过当日14:55:00新入场截止"
        daily.update(rows=[], total=0, execution_status=status("expired", reason))
        daily["section_statuses"] = {**(daily.get("section_statuses") or {}), "buy": "expired"}
        daily["section_errors"] = {**(daily.get("section_errors") or {}), "buy": reason}
        if daily.get("sections") is not None:
            daily["sections"]["buy"] = []
    for key in ("events", "execution_events"):
        result[key] = entry_events_before_deadline(
            result.get(key, []), {**result["results"], **guards}, now,
        )
