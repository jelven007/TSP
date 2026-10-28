"""指数目录及日K同步服务。"""
from __future__ import annotations

import gc
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import polars as pl

from app.indicators.pipeline import compute_enriched
from app.market_time import cn_now
from app.services import kline_sync, preferences
from app.services.instrument_sync import _fetch_instruments_via_provider
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import (
    ResolvedLimit,
    chunked,
    min_batch,
    resolve_limit,
    sleep_between_batches,
)
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ"]


def _quotes_to_index_instruments(resp) -> pl.DataFrame:
    """将行情响应规范为指数 instruments。"""
    if resp is None:
        return pl.DataFrame()
    if isinstance(resp, pl.DataFrame):
        df = resp
    elif hasattr(resp, "columns"):
        df = pl.from_pandas(resp.reset_index() if hasattr(resp, "reset_index") else resp)
    else:
        rows: list[dict] = []
        for quote in resp or []:
            item = quote if isinstance(quote, dict) else {}
            ext = item.get("ext") or {}
            symbol = item.get("symbol")
            if not symbol:
                continue
            rows.append({
                "symbol": str(symbol),
                "name": ext.get("name") or item.get("name") or str(symbol),
            })
        df = pl.DataFrame(rows)
    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()
    df = df.rename({"ts_code": "symbol"} if "ts_code" in df.columns else {})
    if "name" not in df.columns:
        df = df.with_columns(pl.col("symbol").cast(pl.Utf8).alias("name"))
    result = (
        df.select(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("name").cast(pl.Utf8),
        )
        .with_columns(
            pl.col("symbol").str.split(".").list.first().alias("code"),
            pl.lit("index").alias("asset_type"),
        )
    )
    from app.market_scope import filter_market_frame
    return filter_market_frame(result).unique(subset=["symbol"], keep="last").sort("symbol")


def _fetch_instruments_by_type(instrument_type: str, asset_type_label: str) -> pl.DataFrame:
    """通过当前 Provider 或交易所接口拉取指数目录。"""
    custom_rows = _fetch_instruments_via_provider(instrument_type)
    if custom_rows is not None:
        rows = [
            {"symbol": str(row["symbol"]), "name": row.get("name") or str(row["symbol"])}
            for row in custom_rows
            if row.get("symbol") and str(row["symbol"]).upper().endswith((".SH", ".SZ"))
        ]
        if not rows:
            return pl.DataFrame()
        return (
            pl.DataFrame(rows)
            .with_columns(
                pl.col("symbol").str.split(".").list.first().alias("code"),
                pl.lit(asset_type_label).alias("asset_type"),
            )
            .unique(subset=["symbol"], keep="last")
            .sort("symbol")
        )

    client = get_client()
    rows: list[dict] = []
    for exchange in _EXCHANGES:
        try:
            items = client.exchanges.get_instruments(
                exchange, instrument_type=instrument_type,
            )
            for value in items or []:
                item = value if isinstance(value, dict) else {}
                symbol = item.get("symbol")
                if symbol:
                    rows.append({
                        "symbol": str(symbol),
                        "name": item.get("name") or str(symbol),
                    })
        except Exception as exc:
            logger.warning(
                "get_instruments(%s, type=%s) failed: %s",
                exchange, instrument_type, exc,
            )
            return pl.DataFrame()
    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame(rows)
        .with_columns(
            pl.col("symbol").str.split(".").list.first().alias("code"),
            pl.lit(asset_type_label).alias("asset_type"),
        )
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )


def sync_index_instruments(repo: KlineRepository, pull_index: bool = True) -> int:
    """同步指数维表, 数据源目录与产品目录合并。"""
    if not pull_index:
        return 0
    parts: list[pl.DataFrame] = []
    index_df = _fetch_instruments_by_type("index", "index")
    if not index_df.is_empty():
        parts.append(index_df)
    from app.services.index_const import page_index_catalog
    parts.append(pl.DataFrame(page_index_catalog()))

    if preferences.get_daily_data_provider() == "tickflow":
        capset = None
        try:
            from app.tickflow import policy
            capset = policy.detect_capabilities(force=False)
        except Exception:  # noqa: BLE001
            pass
        if capset is not None and capset.has(Cap.QUOTE_POOL):
            client = get_client()
            for kwargs in (
                {"universes": ["CN_Index"]},
                {"universes": ["CN_Index"], "as_dataframe": False},
            ):
                try:
                    response = client.quotes.get_by_universes(**kwargs)
                    if response is not None and len(response) > 0:
                        supplement = _quotes_to_index_instruments(response)
                        if not supplement.is_empty():
                            parts.append(supplement)
                        break
                except Exception as exc:
                    logger.debug("CN_Index universe supplement failed: %s", exc)

    instruments = (
        pl.concat(parts, how="diagonal_relaxed")
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )
    if instruments.is_empty():
        logger.warning("指数标的列表为空")
        return 0
    repo.save_index_instruments(instruments)
    repo.refresh_index_views()
    logger.info("指数标的同步完成: %d 只", instruments.height)
    return instruments.height


def sync_and_persist_index_daily(
    repo: KlineRepository,
    capset: CapabilitySet,
    count: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    symbols_override: list[str] | None = None,
    on_chunk_done: Callable[[int, int], None] | None = None,
) -> int:
    """同步指数日K, 计算通用指标及本地周/月K。"""
    custom_daily = preferences.get_daily_data_provider() != "tickflow"
    if not custom_daily and not capset.has(Cap.KLINE_DAILY_BATCH):
        return 0
    if symbols_override:
        symbols = sorted(set(symbol for symbol in symbols_override if symbol))
    else:
        instruments = repo.get_index_instruments()
        if instruments.is_empty():
            sync_index_instruments(repo)
            instruments = repo.get_index_instruments()
        if instruments.is_empty() or "symbol" not in instruments.columns:
            return 0
        symbols = sorted(set(instruments["symbol"].to_list()))
    if not symbols:
        return 0
    limit = (
        ResolvedLimit(batch=None, rpm=None) if custom_daily
        else resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    )
    batch_size = min_batch(preferences.get_index_daily_batch_size(), limit)
    end_time = end_date or cn_now()
    start_time = start_date or (end_time - timedelta(days=365))
    total_rows = 0
    chunks = chunked(symbols, batch_size)
    for i, chunk in enumerate(chunks):
        sleep_between_batches(i, limit.rpm)
        raw = kline_sync.sync_daily_batch(
            chunk, count=count, batch_size=None,
            start_time=start_time, end_time=end_time, asset_type="index",
        )
        if raw.is_empty():
            continue
        repo.append_index_daily(raw)
        enriched = compute_enriched(raw, factors=None, instruments=None)
        repo.append_index_enriched(enriched)
        from app.services.index_kline import rebuild
        rebuild(repo, chunk, raw["date"].min(), raw["date"].max())
        total_rows += raw.height
        logger.info("index daily synced: %d/%d chunks, +%d rows", i + 1, len(chunks), raw.height)
        if on_chunk_done:
            on_chunk_done(i + 1, len(chunks))
        del raw, enriched
        gc.collect()
    repo.refresh_index_views()
    return total_rows
