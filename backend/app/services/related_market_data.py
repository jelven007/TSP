"""Registered related data, restart-safe backfill and bounded daily maintenance."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.market_time import cn_now, cn_today
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig, write_ext_parquet
from app.services.fs_utils import atomic_write_parquet, atomic_write_text
from app.services.st_history_backfill import parse_special_treatment_events

logger = logging.getLogger(__name__)
START = date(2026, 3, 30)
DATASET_IDS = {
    "money_flow": "ext_money_flow", "announcements": "ext_announcements",
    "st_status": "ext_st_status", "sina_money_flow": "ext_sina_money_flow",
}
_RUN_LOCK = threading.Lock()


def register_datasets(data_dir: Path) -> None:
    store = ExtConfigStore(data_dir)
    common = [
        ExtField("symbol"), ExtField("date"), ExtField("source"), ExtField("observed_at"),
        ExtField("publication_time_verified", "bool"),
    ]
    definitions = {
        "money_flow": ("个股资金流", [
            ExtField(f"{name}_{suffix}", "float")
            for name in ("main", "small", "medium", "large", "super_large")
            for suffix in ("net_inflow", "net_ratio")
        ] + [ExtField("close", "float"), ExtField("change_ratio", "float")]),
        "sina_money_flow": ("个股资金流(新浪口径)", [
            ExtField(name, "float") for name in (
                "net_inflow", "net_ratio", "super_large_net_inflow", "large_net_inflow",
                "small_net_inflow", "retail_net_inflow", "close", "change_ratio",
            )
        ]),
        "announcements": ("股票公告索引", [
            ExtField(name) for name in (
                "announcement_id", "title", "display_time", "categories", "source_url",
            )
        ]),
        "st_status": ("历史 ST 状态", [
            ExtField("is_risk_warning", "bool"), ExtField("status_known", "bool"),
            ExtField("risk_warning_code", "int", "ST状态(1风险警示,0普通)"),
            ExtField("status_known_code", "int", "状态已核实(1是,0否)"),
            ExtField("evidence_sha256"),
        ]),
    }
    for key, (label, fields) in definitions.items():
        existing = store.get(DATASET_IDS[key])
        if existing is not None:
            if key == "st_status":
                names = {field.name for field in existing.fields}
                additions = [field for field in fields if field.name not in names]
                if additions:
                    existing.fields.extend(additions)
                    store.upsert(existing, keep_strategy_cache=True)
            continue
        store.upsert(ExtConfig(
            id=DATASET_IDS[key], label=label, mode="timeseries",
            fields=common + fields, symbol_map={"type": "mapped", "col": "symbol"},
            description=(
                "由 related_market_data 平台任务同步。资金净额为元、比例为小数;"
                "公告为索引, 正文由 source_url 定位; 未知与历史窗口缺口不填零。"
                "观察时间不是历史可用时间, 未验证数据不得用于严格时点回测。"
            ),
            # Composite event key is necessary even without an HTTP pull recipe.
            pull=PullConfig(time_field="announcement_id") if key == "announcements" else None,
        ), keep_strategy_cache=True)
    # Resume partial migrations by checking every partition, even if config saved.
    migrated = False
    for path in (data_dir / "ext_data/ext_st_status/timeseries").glob("date=*/part.parquet"):
        if {"risk_warning_code", "status_known_code"} <= set(pl.read_parquet_schema(path)):
            continue
        atomic_write_parquet(_st_numeric_columns(pl.read_parquet(path)), path)
        migrated = True
    if migrated:
        from app.factors.ext_factors import invalidate_ext_caches

        invalidate_ext_caches(data_dir, keep_strategy_cache=True)


def _st_numeric_columns(frame):
    """Unknown ST evidence stays null, never materializes a normal-stock code."""
    return frame.with_columns(
        pl.when(pl.col("status_known")).then(pl.col("is_risk_warning").cast(pl.Int64))
        .otherwise(None).alias("risk_warning_code"),
        pl.col("status_known").cast(pl.Int64).alias("status_known_code"),
    )


def persist_rows(data_dir: Path, dataset: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    config = ExtConfigStore(data_dir).get(DATASET_IDS[dataset])
    if config is None:
        raise ValueError("相关数据尚未注册")
    schema = {
        field.name: {"float": pl.Float64, "int": pl.Int64, "bool": pl.Boolean}.get(field.dtype, pl.String)
        for field in config.fields
    }
    frame = pl.from_dicts(rows, schema=schema)
    if dataset == "st_status":
        frame = _st_numeric_columns(frame)
    for day in frame["date"].unique().to_list():
        write_ext_parquet(
            frame.filter(pl.col("date") == day), config, data_dir,
            snapshot_date=date.fromisoformat(day), keep_strategy_cache=True,
        )
    return frame.height


def money_flow_gaps(expected: list[date], rows: list[dict]) -> list[str]:
    actual = {row["date"] for row in rows}
    return [str(day) for day in expected if str(day) not in actual]


def st_daily_rows(instrument, text, days, *, observed_at):
    events = parse_special_treatment_events(instrument["symbol"], text)
    return st_event_rows(
        instrument, events, days, observed_at=observed_at,
        source="rustdx_f10", digest=hashlib.sha256(text.encode()).hexdigest(),
    )


def st_event_rows(instrument, events, days, *, observed_at, source, digest):
    events.sort(key=lambda event: (event["effective_date"], event["announcement_date"]))
    rows = []
    listing = instrument.get("listing_date")
    listing = date.fromisoformat(str(listing)[:10]) if listing else None
    # F10's explicit no-data section is an observation, not a missing response.
    current_risk = "ST" in (instrument.get("name") or "").upper()
    if not events and current_risk:
        raise ValueError("ST 当前名称与无事件原文矛盾")
    changes = [event for event in events if event["is_risk_warning_after"] is not None]
    if events and not changes:
        raise ValueError("F10 事件没有可确定的 ST 状态变化")
    initial_state = bool(changes and (
        changes[0]["classification"] in {"warning_removed", "warning_retained"}
        or "继续" in changes[0]["treatment_type"]
    ))
    current_state = initial_state
    for event in changes:
        if event["effective_date"] <= cn_today():
            current_state = event["is_risk_warning_after"]
    if current_state != current_risk:
        raise ValueError("ST 事件最新状态与当前名称矛盾")
    for day in days:
        if listing and day < listing:
            continue
        state = initial_state
        for event in events:
            if event["effective_date"] > day:
                break
            if event["is_risk_warning_after"] is not None:
                state = event["is_risk_warning_after"]
        rows.append({
            "symbol": instrument["symbol"], "date": str(day), "is_risk_warning": state,
            "status_known": True, "source": source, "observed_at": observed_at,
            "publication_time_verified": False, "evidence_sha256": digest,
        })
    return rows


def _state_path(data_dir):
    return data_dir / "user_data" / "related_market_data_sync.json"


def _save_state(data_dir, state):
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))


def _load_state(data_dir):
    path = _state_path(data_dir)
    return json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "datasets": {}}


def _expected_days(data_dir, start, end):
    # Authoritative local daily rows: no invented trading calendar or IPO days.
    files = [
        str(p) for p in (data_dir / "kline_daily").glob("date=*/part.parquet")
        if str(start) <= p.parent.name[5:] <= str(end)
    ]
    if not files:
        raise ValueError("指定区间无本地日线, 不能声明股票日期覆盖")
    frame = pl.scan_parquet(
        files, hive_partitioning=False, schema={"symbol": pl.String, "date": pl.Date},
        extra_columns="ignore",
    ).collect()
    return {
        symbol: sorted(group["date"].unique().to_list())
        for (symbol,), group in frame.group_by("symbol")
    }


def sync_related_data(
    data_dir: Path, *, start: date = START, end: date | None = None,
    datasets=("sina_money_flow", "money_flow", "announcements", "st_status"), workers: int = 4,
    refresh: bool = False,
):
    """Checkpoint each bounded batch; successful fetch is not complete coverage."""
    from app.data_providers import custom

    data_dir, end = Path(data_dir), end or cn_today()
    if start > end or not set(datasets) <= set(DATASET_IDS):
        raise ValueError("相关数据区间或数据集非法")
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "already_running"}
    try:
        return _sync(data_dir, start, end, datasets, max(1, min(workers, 8)), refresh, custom)
    finally:
        _RUN_LOCK.release()


@contextmanager
def _process_lock(data_dir):
    import os

    path = data_dir / "user_data/.related_market_data.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _sync(data_dir, start, end, datasets, workers, refresh, custom):
    # CLI and platform scheduler share a cross-process lock.
    with _process_lock(data_dir) as acquired:
        if not acquired:
            return {"skipped": "already_running"}
        register_datasets(data_dir)
        expected = _expected_days(data_dir, start, end)
        instrument_frame = pl.read_parquet(data_dir / "instruments/instruments.parquet")
        inst = instrument_frame.to_dicts()
        lookup = {row["symbol"][:6]: row["symbol"] for row in inst}
        instruments = {row["symbol"]: row for row in inst}
        symbols = sorted(instruments)
        state = _load_state(data_dir)
        state.update({"start": str(start), "end": str(end), "universe_count": len(symbols)})
        from app.services.listing_share_capital import (
            fetch_listing_capital,
            missing_listing_instruments,
            persist_listing_events,
        )

        missing_capital = missing_listing_instruments(data_dir, instrument_frame, expected)
        if missing_capital:
            capital = {"success_items": 0, "errors": {}}
            provider = custom.get_provider("public_disclosures")
            for instrument in missing_capital:
                try:
                    record = fetch_listing_capital(provider, instrument)
                    persist_listing_events(data_dir, [record])
                    capital["success_items"] += 1
                except Exception as exc:
                    capital["errors"][instrument["symbol"]] = str(exc)
                state["listing_share_capital"] = capital
                _save_state(data_dir, state)
        for dataset in datasets:
            progress = state["datasets"].setdefault(dataset, {"items": {}})
            progress["status"] = "running"
            _save_state(data_dir, state)
            if dataset == "announcements":
                provider = custom.get_provider("public_disclosures")
                day = start
                while day <= end:
                    key = str(day)
                    old = progress["items"].get(key, {})
                    if old.get("status") == "success" and not refresh and day < end - timedelta(days=7):
                        day += timedelta(days=1)
                        continue
                    try:
                        rows = provider.get_announcements(day, lookup)
                        count = persist_rows(data_dir, dataset, rows)
                        progress["items"][key] = {"status": "success", "rows": count}
                    except Exception as exc:
                        progress["items"][key] = {"status": "error", "error": str(exc)}
                    _save_state(data_dir, state)
                    logger.info("related_data announcements %s %s", key, progress["items"][key]["status"])
                    day += timedelta(days=1)
            else:
                provider = custom.get_provider("rustdx" if dataset == "st_status" else "public_disclosures")
                pending = [
                    symbol for symbol in symbols
                    if refresh or progress["items"].get(symbol, {}).get("end") != str(end)
                    or progress["items"].get(symbol, {}).get("status") == "error"
                ]
                def fetch(symbol, dataset=dataset, provider=provider):
                    if dataset == "money_flow":
                        return symbol, provider.get_money_flow(symbol, start, end), None
                    if dataset == "sina_money_flow":
                        return symbol, provider.get_sina_money_flow(symbol, start, end), None
                    raw = provider.get_special_treatment_history([symbol])[0]
                    if raw.get("error"):
                        raise ValueError(raw["error"])
                    try:
                        rows = st_daily_rows(
                            instruments[symbol], raw["text"], expected.get(symbol, []),
                            observed_at=raw["observed_at"],
                        )
                    except ValueError:
                        from app.services.issuer_st_status import fetch_issuer_st_evidence

                        evidence = fetch_issuer_st_evidence(
                            custom.get_provider("public_disclosures"), instruments[symbol], start, end,
                        )
                        rows = st_event_rows(
                            instruments[symbol], evidence["events"], expected.get(symbol, []),
                            observed_at=evidence["observed_at"], source=evidence["source"],
                            digest=evidence["evidence_sha256"],
                        )
                        evidence["f10"] = raw
                        raw = evidence
                    return symbol, rows, raw

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    # Bounded submission avoids retaining every full-universe result.
                    for offset in range(0, len(pending), 64):
                        buffer = []
                        futures = {pool.submit(fetch, symbol): symbol for symbol in pending[offset:offset + 64]}
                        for future in as_completed(futures):
                            symbol = futures[future]
                            try:
                                _, rows, raw = future.result()
                                missing = money_flow_gaps(expected.get(symbol, []), rows)
                                progress["items"][symbol] = {
                                    "status": "partial" if missing else "success", "end": str(end),
                                    "rows": len(rows), "missing_dates": missing,
                                }
                                if raw is not None:
                                    if raw.get("source") == "issuer_disclosure":
                                        from app.services.issuer_st_status import (
                                            persist_issuer_st_evidence,
                                        )

                                        persist_issuer_st_evidence(data_dir, symbol, raw)
                                    else:
                                        archive = data_dir / "instrument_status/f10" / symbol
                                        archive.mkdir(parents=True, exist_ok=True)
                                        digest = hashlib.sha256(raw["text"].encode()).hexdigest()
                                        if not (archive / f"{digest}.json").exists():
                                            atomic_write_text(archive / f"{digest}.json", json.dumps(raw, ensure_ascii=False))
                                buffer.extend(rows)
                            except Exception as exc:
                                progress["items"][symbol] = {"status": "error", "error": str(exc)}
                        persist_rows(data_dir, dataset, buffer)
                        _save_state(data_dir, state)
                        logger.info("related_data %s %s/%s", dataset, min(offset + 64, len(pending)), len(pending))
            items = list(progress["items"].values())
            progress["status"] = (
                "partial" if any(x["status"] != "success" for x in items) else "success"
            )
            progress["last_attempt"] = datetime.now(UTC).isoformat()
            progress["success_items"] = sum(x["status"] == "success" for x in items)
            progress["error_items"] = sum(x["status"] == "error" for x in items)
            progress["rows"] = sum(x.get("rows", 0) for x in items)
            _save_state(data_dir, state)
        return state


class RelatedMarketDataScheduler:
    def __init__(self):
        self.task = None

    def start(self, data_dir):
        self.task = asyncio.create_task(self._loop(data_dir))

    def stop(self):
        if self.task:
            self.task.cancel()
            self.task = None

    async def _loop(self, data_dir):
        from app.services import preferences

        while True:
            await asyncio.sleep(60)
            if not preferences.get_data_sync_auto_enabled():
                continue
            # An early startup must not consume the day's post-close refresh.
            if cn_now().hour < 18:
                continue
            try:
                state = _load_state(data_dir)
                attempted = state.get("maintenance_date")
                if attempted == str(cn_today()):
                    continue
                result = await asyncio.to_thread(sync_related_data, data_dir)
                if "skipped" not in result:
                    result["maintenance_date"] = str(cn_today())
                    _save_state(data_dir, result)
            except Exception:
                logger.exception("related market data maintenance failed")


related_market_data_scheduler = RelatedMarketDataScheduler()
