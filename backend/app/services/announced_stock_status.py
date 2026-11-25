"""Confirmed stock status at an explicit observation time and effective date."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.market_time import as_cn_datetime
from app.services.fs_utils import atomic_write_parquet, atomic_write_text

RISK_PATH = Path("instrument_status/risk_warning_announcements.parquet")
MORNING_PATH = Path("instrument_status/morning_check.json")
RISK_SCHEMA = {
    "symbol": pl.String, "effective_date": pl.Date, "announced_on": pl.Date,
    "announced_at": pl.Datetime("us"), "is_risk_warning": pl.Boolean,
    "source_url": pl.String,
    "evidence_sha256": pl.String,
}
_lock = threading.Lock()


def load_risk_events(data_dir):
    path = Path(data_dir) / RISK_PATH
    if not path.exists():
        return pl.DataFrame(schema=RISK_SCHEMA)
    frame = pl.read_parquet(path).select(*RISK_SCHEMA).cast(RISK_SCHEMA)
    if (any(frame[name].null_count() for name in RISK_SCHEMA)
            or frame.unique(["symbol", "effective_date", "announced_on"]).height != frame.height
            or frame.filter(pl.col("announced_at").dt.date() != pl.col("announced_on")).height):
        raise ValueError("ST公告状态证据无效")
    return frame


def merge_risk_events(repo, events):
    from app.services.kline_sync import _minute_partition_lock

    if not events:
        return 0
    data_dir = repo.store.data_dir
    path = data_dir / RISK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(not event.get("evidence_text") for event in events):
        raise ValueError("ST公告缺少已核验正文证据")
    candidate = pl.from_dicts([{
        **event, "evidence_sha256": hashlib.sha256(event["evidence_text"].encode()).hexdigest(),
    } for event in events], schema=RISK_SCHEMA)
    if (any(candidate[name].null_count() for name in RISK_SCHEMA)
            or candidate.filter(~pl.col("source_url").str.starts_with("https://")
                                | (pl.col("announced_at").dt.date() != pl.col("announced_on"))).height):
        raise ValueError("新增ST公告证据字段无效")
    with _lock, _minute_partition_lock(path):
        history = load_risk_events(data_dir)
        keys = ("symbol", "effective_date", "announced_on")
        saved = {tuple(row[key] for key in keys): row for row in history.iter_rows(named=True)}
        added = []
        for row in candidate.iter_rows(named=True):
            key = tuple(row[name] for name in keys)
            old = saved.get(key)
            if old is not None:
                if old["is_risk_warning"] != row["is_risk_warning"]:
                    raise ValueError(f"ST公告证据冲突: {row['symbol']} {row['effective_date']}")
                continue
            saved[key] = row
            added.append(row)
        for event in events:
            text = event.get("evidence_text")
            if text:
                digest = hashlib.sha256(text.encode()).hexdigest()
                archive = data_dir / "instrument_status/announcement_evidence" / f"{digest}.json"
                archive.parent.mkdir(parents=True, exist_ok=True)
                if not archive.exists():
                    atomic_write_text(archive, json.dumps({
                        "text": text, "sha256": digest, "source_url": event["source_url"],
                        "symbol": event["symbol"], "announced_at": str(event["announced_at"]),
                    }, ensure_ascii=False))
        if added:
            frame = pl.concat([history, pl.from_dicts(added, schema=RISK_SCHEMA)])
            atomic_write_parquet(frame.sort(list(keys)), path)
        return len(added)


def _visible(history, target, observed_at):
    if history.is_empty():
        return history
    wall = as_cn_datetime(observed_at).replace(tzinfo=None)
    if target < wall.date():
        raise ValueError("实时状态刷新不能回写已过去交易日")
    return (history.filter((pl.col("effective_date") <= target)
                           & (pl.col("announced_at") <= wall))
            .sort(["symbol", "effective_date", "announced_at"])
            .unique("symbol", keep="last", maintain_order=True))


def confirmed_status(repo, target: date, *, observed_at: datetime):
    """Unknown status is absent; old/future public clocks cannot leak into this view."""
    from app.instrument_suspension import load_suspension_history

    suspension = _visible(load_suspension_history(repo.store.data_dir), target, observed_at)
    risk = _visible(load_risk_events(repo.store.data_dir), target, observed_at)
    result = {"target_date": str(target), "observed_at": as_cn_datetime(observed_at).isoformat()}
    for frame, column, positive, negative in [
        (suspension, "is_suspended", "suspended", "resumed"),
        (risk, "is_risk_warning", "risk_warning", "risk_removed"),
    ]:
        result[positive] = sorted(frame.filter(pl.col(column))["symbol"].to_list())
        result[negative] = sorted(frame.filter(~pl.col(column))["symbol"].to_list())
    result["evidence_digest"] = hashlib.sha256(
        json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def morning_status(repo, target, *, observed_at, slot):
    """Consumers use this batch's published scan, never yesterday's check."""
    from app.services.daily_pool_schedule import scheduled_for

    wall = as_cn_datetime(observed_at)
    try:
        receipt = json.loads((repo.store.data_dir / MORNING_PATH).read_text())
        scheduled = scheduled_for(target, slot)
        if (receipt["scheduled_for"] != scheduled or receipt["target_date"] != str(target)
                or not datetime.fromisoformat(scheduled) <= datetime.fromisoformat(receipt["cutoff"]) <= wall
                or not datetime.fromisoformat(scheduled) <= datetime.fromisoformat(receipt["finished_at"]) <= wall
                or receipt["status"] in ("running", "cancelled", "interrupted")
                or receipt.get("publish_pending")):
            raise ValueError("股票状态本轮扫描尚未完成发布")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("股票状态缺少本轮已发布扫描回执") from exc
    return {**receipt, **confirmed_status(repo, target, observed_at=wall)}
