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
