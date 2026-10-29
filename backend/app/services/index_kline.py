"""指数独立日/周/月K: 本地聚合、周期指标与七只标的的有界修正。"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time, timedelta

import polars as pl

from app.market_time import cn_now, cn_today
from app.services import kline_sync
from app.services.index_const import PAGE_INDEX_SYMBOLS, page_index_catalog
from app.services.kline_gaps import _trading_days
from app.services.realtime_guard import pause_quotes_for_write, paused_quotes

logger = logging.getLogger(__name__)
TABLES = {"1d": "kline_index_daily", "1w": "kline_index_weekly", "1mo": "kline_index_monthly"}
LABELS = {"1d": "日K", "1w": "周K", "1mo": "月K"}
VALUES = ("open", "high", "low", "close", "volume", "amount")


def available_range(repo, period: str) -> tuple[date | None, date | None]:
    """返回本地指定周期实际存在的分区范围。"""
    if period not in TABLES:
        raise ValueError("不支持的指数K线周期")
    table = "kline_index_enriched" if period == "1d" else TABLES[period]
    days = []
    for partition in (repo.store.data_dir / table).glob("date=*"):
        if not partition.is_dir() or not any(partition.glob("*.parquet")):
            continue
        try:
            days.append(date.fromisoformat(partition.name[5:]))
        except ValueError:
            continue
    if not days and period != "1d":
        return available_range(repo, "1d")
    return (min(days), max(days)) if days else (None, None)


def bucket(day: date, period: str) -> date:
    if period not in TABLES:
        raise ValueError("不支持的指数K线周期")
    return day - timedelta(days=day.weekday()) if period == "1w" else day.replace(day=1) if period == "1mo" else day


def bucket_end(day: date, period: str) -> date:
    if period == "1w":
        return bucket(day, period) + timedelta(days=6)
    if period == "1mo":
        return day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return day


def aggregate(frame: pl.DataFrame, period: str) -> pl.DataFrame:
    if period not in ("1w", "1mo"):
        raise ValueError("只能聚合指数周K或月K")
    frame = kline_sync._normalize_daily(frame)
    if frame.is_empty():
        return frame
    for column in VALUES:
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    label = (
        pl.col("date") - pl.duration(days=pl.col("date").dt.weekday() - 1)
        if period == "1w" else pl.col("date").dt.month_start()
    )
    frame = frame.with_columns(
        label.cast(pl.Date).alias("_bucket"),
        *[pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in VALUES],
    ).unique(["symbol", "date"], keep="last").sort("symbol", "date")

    def finite(column):
        return pl.col(column).filter(pl.col(column).is_finite())

    return frame.group_by("symbol", "_bucket", maintain_order=True).agg(
        pl.col("date").last().alias("trade_date"),
        pl.col("open").first(), finite("high").max().alias("high"),
        finite("low").min().alias("low"), pl.col("close").last(),
        *[pl.when(finite(c).count() > 0).then(finite(c).sum()).otherwise(None).alias(c) for c in ("volume", "amount")],
    ).rename({"_bucket": "date"}).sort("symbol", "date")


def rebuild(repo, symbols: list[str], start: date, end: date, *, periods=("1w", "1mo"), only_missing=False, excluded_keys=None) -> int:
    """包含起止所在整桶; 不把周中修正误写成一根残缺周线。"""
    rows = 0
    for period in periods:
        lo, hi = bucket(start, period), min(bucket_end(end, period), cn_today())
        raw = repo.read_index_frame("1d", symbols, lo, hi)
        derived = aggregate(raw, period)
        if excluded_keys is not None and not derived.is_empty():
            derived = derived.join(excluded_keys, on=["symbol", "date"], how="anti")
        if not derived.is_empty():
            with pause_quotes_for_write():
                rows += repo.append_index_period(derived, period, only_missing=only_missing)
    return rows


def repair(repo, capset, start: date, end: date, *, period="1d", mode="full", on_progress=None, quote_service=None) -> dict:
    from app.services.pipeline_jobs import JobCancelledError
    if period not in TABLES or mode not in ("full", "missing"):
        return {"error": "不支持的周期或获取方式"}
    if start > end or end > cn_today():
        return {"error": "修正日期范围无效"}
    symbols = list(PAGE_INDEX_SYMBOLS)
    result = {
        "period": period, "mode": mode, "rows": 0, "universe_size": len(symbols),
        "start": str(start), "end": str(end), "source": "1d" if period != "1d" else "daily",
        "missing_before": 0, "remaining": 0, "requests": 0,
    }

    def emit(stage, percent, message):
        if on_progress:
            on_progress(stage, percent, message, stage_pct=percent)

    with paused_quotes(quote_service):
        try:
            emit("init", 2, f"检查七只指数的{LABELS[period]}")
            # 手工修正只补固定目录, 不远程同步/改写用户的其他目录。
            current = repo.get_index_instruments()
            catalog = pl.DataFrame(page_index_catalog())
            if not current.is_empty():
                catalog = pl.concat([current, catalog], how="diagonal_relaxed").unique("symbol", keep="last")
            with pause_quotes_for_write():
                repo.save_index_instruments(catalog)
            lo = bucket(start, period)
            days = _trading_days(lo, end, local_only=period != "1d")
            now = cn_now().replace(tzinfo=None)
            # 当前日线在收盘前尚未完整; 遗漏模式不把盘中尚未形成的记录算缺口。
            if mode == "missing" and period == "1d":
                days = [d for d in days if d < now.date() or now.time() >= time(15)]
            expected = pl.DataFrame({
                "symbol": symbols, "date": [lo] * len(symbols),
            }, schema={"symbol": pl.String, "date": pl.Date}).select("symbol").join(
                pl.DataFrame({"date": sorted({bucket(d, period) for d in days})}, schema={"date": pl.Date}),
                how="cross",
            )
            existing = repo.read_index_frame(period, symbols, lo, end)
            existing_keys = existing.select("symbol", "date") if not existing.is_empty() else expected.head(0)
            gaps = expected.join(existing_keys, on=["symbol", "date"], how="anti")
            result["missing_before"] = gaps.height
            if period != "1d":
                emit("index_aggregate", 30, f"从本地日K计算七只指数的{LABELS[period]}")
                base_end = min(bucket_end(end, period), cn_today())
                base_days = _trading_days(lo, base_end, local_only=True)
                if now.time() < time(15):
                    base_days = [day for day in base_days if day < now.date()]
                expected_base = expected.select("symbol").unique().join(
                    pl.DataFrame({"date": base_days}, schema={"date": pl.Date}), how="cross",
                )
                raw = repo.read_index_frame("1d", symbols, lo, base_end)
                base_keys = raw.select("symbol", "date") if not raw.is_empty() else expected_base.head(0)
                base_gaps = expected_base.join(base_keys, on=["symbol", "date"], how="anti")
                result["base_remaining"] = base_gaps.height
                incomplete = base_gaps.with_columns(
                    (pl.col("date") - pl.duration(days=pl.col("date").dt.weekday() - 1)).cast(pl.Date).alias("date")
                    if period == "1w" else pl.col("date").dt.month_start().alias("date"),
                ).unique(["symbol", "date"])
                result["rows"] = rebuild(
                    repo, symbols, start, end, periods=(period,), only_missing=mode == "missing",
                    excluded_keys=incomplete if mode == "missing" else None,
                )
                if base_gaps.height:
                    result["error"] = f"本地指数日K仍缺{base_gaps.height}条, 请先补齐日K; 已写入{result['rows']}条{LABELS[period]}"
            else:
                # 全量按有界日期段批量获取; 遗漏只请求同日缺失证券。
                units = []
                if mode == "missing":
                    units = [(df["date"][0], df["date"][0], df["symbol"].to_list()) for df in gaps.sort("date", "symbol").partition_by("date")]
                else:
                    cursor = start
                    while cursor <= end:
                        hi = min(end, cursor + timedelta(days=365))
                        units.append((cursor, hi, symbols))
                        cursor = hi + timedelta(days=1)
                failures = 0
                for i, (a, b, batch) in enumerate(units):
                    pct = 5 + int(75 * i / max(1, len(units)))
                    emit("index_fetch", pct, f"获取 {a}~{b} 的 {len(batch)} 只指数")
                    result["requests"] += 1
                    try:
                        raw = kline_sync.sync_daily_batch(
                            batch, start_time=datetime.combine(a, time.min),
                            end_time=datetime.combine(b, time.max), asset_type="index",
                        )
                    except JobCancelledError:
                        raise
                    except Exception:
                        failures += 1
                        logger.warning("指数日K批次获取失败", exc_info=True)
                        continue
                    emit("index_fetch", pct, f"保存 {a}~{b} 的指数日K")
                    if raw.is_empty():
                        continue
                    raw = raw.filter(pl.col("symbol").is_in(batch) & pl.col("date").is_between(a, b))
                    if mode == "missing":
                        raw = raw.join(gaps, on=["symbol", "date"], how="inner")
                    if raw.is_empty():
                        continue
                    with pause_quotes_for_write():
                        written = repo.append_index_period(raw, "1d", only_missing=mode == "missing")
                        # 读时计算通用指标, 不借用股票复权/股本逻辑。
                        persisted = repo.read_index_frame("1d", batch, a, b)
                        repo.append_index_enriched(persisted)
                    result["rows"] += written
                    emit("index_aggregate", 82, "更新受影响指数的周K、月K")
                    rebuild(repo, batch, a, b)
                if failures:
                    result["error"] = f"指数日K有 {failures} 批获取失败, 已写入{result['rows']}行"
            emit("refresh_views", 95, "检查持久数据并刷新指数视图")
            persisted = repo.read_index_frame(period, symbols, lo, end)
            keys = persisted.select("symbol", "date") if not persisted.is_empty() else expected.head(0)
            result["remaining"] = expected.join(keys, on=["symbol", "date"], how="anti").height
            if result["remaining"] and not result.get("error"):
                result["error"] = f"已写入{result['rows']}行, 仍有{result['remaining']}条未补齐; " + (
                    "请先补齐本地指数日K" if period != "1d" else "数据源未返回部分指数或日期的数据"
                )
            elif not result.get("error") and mode == "full" and result["rows"] == 0:
                result["error"] = "选定区间没有可修正数据"
            elif not result.get("error"):
                result["message"] = "选定区间没有遗漏数据, 无需获取" if mode == "missing" and gaps.is_empty() else f"七只指数{LABELS[period]}修正完成, 已写入{result['rows']}行"
            return result
        finally:
            repo.refresh_index_views()
            repo._refresh_index_enriched()
            from app.services.public_query_cache import (
                invalidate_data_cache,
                invalidate_storage_cache,
            )

            invalidate_data_cache("index")
            invalidate_storage_cache()
            if quote_service is not None:
                quote_service._broadcast_quote_updated()
