"""Read the evidence used by the Data overview without running any scheduled job."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from functools import lru_cache

from app.market_time import as_cn_datetime, published_trading_day
from app.services.announced_stock_status import MORNING_PATH
from app.services.daily_pool_schedule import SLOTS, due_slot
from app.services.post_close_integrity import REVIEW_TIME
from app.services.post_close_sync import SYNC_TIME

EVENING = time(20, 30)
MORNING_DEADLINE = time(9, 30)
MARKET_CLOSE = time(15)
RECEIPT_STATES = {
    "queued", "pending", "running", "succeeded", "failed", "cancelled", "interrupted", "unavailable",
}


def _receipt(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"status": "unavailable"}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"status": "unavailable"}


def _stamp(value):
    try:
        return as_cn_datetime(datetime.fromisoformat(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _nearby(day, step):
    # Refuse to cross an unpublished part of the calendar, including year-end.
    for _ in range(14):
        day += timedelta(days=step)
        verdict = published_trading_day(day)
        if verdict is not False:
            return day if verdict is True else None
    return None


def _state(value):
    return value if isinstance(value, str) and value in RECEIPT_STATES else "not_run"


def _count(value):
    return value if type(value) is int and value >= 0 else None


@lru_cache(maxsize=4)
def _evening_receipt(path, file_identity):
    """Do not repeatedly decode the unrelated market maintenance history."""
    batch = _receipt(path)
    status = batch.get("stock_status")
    if not isinstance(status, dict):
        return {"status": "unavailable"} if batch.get("status") == "unavailable" else {}
    return {key: status.get(key) for key in (
        "target_date", "scheduled_for", "cutoff", "observed_at", "status", "evidence_policy",
        "publish_pending", "coverage_complete", "covered_count", "pending_count",
        "universe_count", "unscanned_count", "unresolved_count", "next_retry_at",
    )}


def _stock_status(data_dir, target, now, active_today, enabled):
    result = {"target_date": target, "status": "calendar_unknown", "coverage": "unverified"}
    if target is None:
        return result
    wall = now.time()
    slot = due_slot(now) if active_today else None
    deadline = datetime.combine(target, MORNING_DEADLINE, now.tzinfo)
    if slot is not None:
        scheduled = datetime.combine(target, time.fromisoformat(slot), now.tzinfo)
        receipt = _receipt(data_dir / MORNING_PATH)
    else:
        path = data_dir / "user_data/related_market_data_sync.json"
        try:
            info = path.stat()
            receipt = _evening_receipt(path, (info.st_ino, info.st_mtime_ns, info.st_size))
        except FileNotFoundError:
            receipt = {}
        except OSError:
            receipt = {"status": "unavailable"}
        scheduled = _stamp(receipt.get("scheduled_for"))
    result.update(status="not_run", scheduled_for=scheduled if slot else None,
                  deadline_elapsed=active_today and wall >= MORNING_DEADLINE)
    if receipt.get("status") == "unavailable":
        return {**result, "status": "unavailable"}
    cutoff = _stamp(receipt.get("cutoff"))
    observed = _stamp(receipt.get("finished_at") if slot else receipt.get("observed_at"))
    if (receipt.get("target_date") != target.isoformat() or not scheduled or not cutoff or not observed
            or _stamp(receipt.get("scheduled_for")) != scheduled or not scheduled <= cutoff <= observed <= now
            or (slot is not None and observed >= deadline)
            or (slot is None and (scheduled.time() != EVENING or scheduled.date() >= target))):
        return result

    status = _state(receipt.get("status"))
    covered = _count(receipt.get("covered_count"))
    coverage = "unverified"
    if receipt.get("evidence_policy") == "stock_status_v2":
        if receipt.get("publish_pending"):
            coverage = "pending_publication"
        elif status == "succeeded" and receipt.get("coverage_complete") is True:
            coverage = "complete"
        elif covered:
            coverage = "partial"
    retry = _stamp(receipt.get("next_retry_at"))
    return {
        **result, "status": status, "coverage": coverage, "scheduled_for": scheduled,
        "observed_at": observed, "cutoff": cutoff,
        "next_retry_at": retry if enabled and retry and now < retry < deadline and coverage != "complete" else None,
        "counts": {"universe": _count(receipt.get("universe_count")),
                   "covered": covered, "pending": _count(receipt.get("pending_count")),
                   "unscanned": _count(receipt.get("unscanned_count")),
                   "unresolved": _count(receipt.get("unresolved_count"))},
    }


def _post_close(data_dir, day, integrity):
    sync = _receipt(data_dir / "post_close_sync/current.json")
    if day is None or (sync.get("day") != day.isoformat() and sync.get("status") != "unavailable"):
        sync = {}
    review = integrity if day and integrity.get("day") == day.isoformat() else {}
    counts = review.get("coverage") or {}
    period = (sync.get("periods") or {}).get("1m") if isinstance(sync.get("periods"), dict) else {}
    return {
        "trade_date": day,
        "sync": {"status": _state(sync.get("status")), "updated_at": _stamp(sync.get("updated_at")),
                 "minute_gaps_at_check": _count(period.get("remaining")) if isinstance(period, dict) else None},
        "daily": {"status": _state(sync.get("daily_settlement_status")),
                  "release_id": sync.get("daily_settlement_release_id")},
        "minute": {"status": _state(sync.get("minute_finalization_status"))},
        "review": {
            "status": _state(review.get("status")),
            "coverage": review.get("completion", "unverified"),
            "valid": _count(counts.get("valid_count")),
            "invalid": _count(counts.get("invalid_count")),
            "remaining": _count(counts.get("remaining")),
            "suspended": _count(counts.get("suspended_skipped")),
            "finished_at": _stamp(review.get("last_finished_at")),
            "next_retry_at": _stamp(review.get("next_retry_at")) if review.get("retry_eligible") else None,
        },
    }


def _next_check(now, today_is_open, next_day):
    slots = [(EVENING, "evening_status")]
    intraday = [(time.fromisoformat(slot), "morning_status") for slot in SLOTS]
    intraday.extend([(SYNC_TIME, "close_sync"), (REVIEW_TIME, "close_review")])
    if today_is_open:
        slots.extend(intraday)
    candidates = [(datetime.combine(now.date(), clock, now.tzinfo), kind) for clock, kind in slots]
    if next_day:
        candidates.extend((datetime.combine(next_day, clock, now.tzinfo), kind) for clock, kind in intraday)
    candidates.append((datetime.combine(now.date() + timedelta(days=1), EVENING, now.tzinfo), "evening_status"))
    due, kind = min(row for row in candidates if row[0] > now)
    return {"kind": kind, "due_at": due}


def summarize(data_dir, integrity, *, now):
    now = as_cn_datetime(now)
    day = now.date()
    enabled = integrity["enabled"]
    open_day = published_trading_day(day)
    next_day = _nearby(day, 1) if open_day is not None else None
    active_today = open_day is True and now.time() < MARKET_CLOSE
    latest_close = day if open_day is True and not active_today else _nearby(day, -1)
    return {
        "observed_at": now, "automatic_enabled": enabled,
        "stock_status": _stock_status(data_dir, day if active_today else next_day, now, active_today, enabled),
        "post_close": _post_close(data_dir, latest_close, integrity),
        "next_check": _next_check(now, open_day, next_day) if enabled and open_day is not None else None,
    }
