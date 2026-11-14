"""Targeted official halt verification and conflict-safe atomic event merge."""
from __future__ import annotations

import threading
from datetime import datetime

import polars as pl

from app.instrument_suspension import (
    RELATIVE_PATH,
    SCHEMA,
    STATUS_COLUMN,
    TIMESTAMP_SCHEMA,
    dated_status,
    load_suspension_history,
)
from app.price_limits import parse_listing_date
from app.services.fs_utils import atomic_write_parquet
from app.services.selection_clock import SELECTION_TIME

_merge_lock = threading.Lock()


def merge_events(repo, events):
    from app.services.kline_sync import _minute_partition_lock

    if not events:
        return 0
    path = repo.store.data_dir / RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = pl.DataFrame(events, schema={**SCHEMA, **TIMESTAMP_SCHEMA})
    if any(candidate[name].null_count() for name in SCHEMA) or candidate.filter(
            ~pl.col("source_url").str.starts_with("https://")
            | (pl.col("announced_at").dt.date() != pl.col("announced_on"))).height:
        raise ValueError("新增停复牌证据字段无效")
    with _merge_lock, _minute_partition_lock(path):
        # Network work precedes this lock. Reread to preserve concurrent additions.
        history = load_suspension_history(repo.store.data_dir)
        saved = {tuple(row[key] for key in ("symbol", "effective_date", "announced_on")): row
                 for row in history.iter_rows(named=True)}
        added = []
        for row in candidate.iter_rows(named=True):
            key = tuple(row[name] for name in ("symbol", "effective_date", "announced_on"))
            old = saved.get(key)
            if old is not None:
                if old["is_suspended"] != row["is_suspended"]:
                    raise ValueError(f"停复牌证据冲突: {row['symbol']} {row['effective_date']}")
                # Legacy unknown publication time and its source remain immutable.
                continue
            saved[key] = row
            added.append(row)
        if added:
            frame = pl.concat([history, pl.DataFrame(added, schema={**SCHEMA, **TIMESTAMP_SCHEMA})])
            atomic_write_parquet(frame.sort(["symbol", "effective_date", "announced_on"]), path)
        return len(added)


def daily_symbols(repo, day):
    """Use current directory identity; only listing date affects expected daily keys."""
    from app.services.instrument_sync import (
        InstrumentSnapshotUnavailableError,
        load_current_stock_pool,
    )
    try:
        snapshot = load_current_stock_pool(repo.store.data_dir)
    except InstrumentSnapshotUnavailableError:
        return []
    instruments = repo.get_instruments()
    listed = ({symbol: parse_listing_date(value) for symbol, value
               in instruments.select("symbol", "listing_date").iter_rows()}
              if "listing_date" in instruments.columns else {})
    return [symbol for symbol in snapshot.symbols
            if listed.get(symbol) is None or listed[symbol] <= day]


def missing_symbols(repo, day):
    from app.services.kline_gaps import _keys
    return sorted(set(daily_symbols(repo, day)) - set(_keys(repo, "1d", day)["symbol"]))


def suspended_symbols(repo, day, *, observed_at=None):
    status = dated_status(repo.get_suspension_history(), day,
                          observed_at=observed_at or datetime.combine(day, SELECTION_TIME))
    return set(status.filter(pl.col(STATUS_COLUMN))["symbol"])


def repair_suspensions(repo, day, *, check_cancelled, lookup=None, max_symbols=32, offset=0):
    from app.data_providers.suspension_announcements import AnnouncementLookup
    from app.services.pipeline_jobs import JobCancelledError

    check_cancelled()
    missing = missing_symbols(repo, day)
    known = suspended_symbols(repo, day) & set(daily_symbols(repo, day))
    candidates = sorted(set(missing)) + sorted(known - set(missing))
    if candidates:
        offset %= len(candidates)
        candidates = candidates[offset:] + candidates[:offset]
    result = {"checked_symbols": [], "added": 0, "diagnostics": {},
              "deferred_symbols": candidates[max_symbols:]}
    if not candidates:
        return {**result, "remaining_symbols": []}
    owned = lookup is None
    if owned:
        lookup = AnnouncementLookup(check_cancelled=check_cancelled)
    try:
        for symbol in candidates[:max_symbols]:
            check_cancelled()
            try:
                response = lookup(symbol, day, cutoff=datetime.combine(day, SELECTION_TIME))
                check_cancelled()
                events = [event for event in response.get("events", [])
                          if event["symbol"] == symbol and event["effective_date"] <= day
                          and event.get("announced_at") is not None
                          and event["announced_at"] <= datetime.combine(day, SELECTION_TIME)]
                result["added"] += merge_events(repo, events)
                result["diagnostics"][symbol] = response.get("diagnostics", [])
            except JobCancelledError:
                raise
            except Exception as exc:
                # Cancellation/expired-window checks remain outside the source-error handler.
                check_cancelled()
                result["diagnostics"][symbol] = [str(exc)]
            result["checked_symbols"].append(symbol)
    finally:
        if owned:
            lookup.close()
    check_cancelled()
    result["remaining_symbols"] = sorted(set(missing_symbols(repo, day)) - suspended_symbols(repo, day))
    return result
