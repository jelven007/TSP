"""按交易日、证券与收盘标签补采缺失 K 线, 扫描和请求均有界。"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta

import polars as pl

from app import market_time
from app.price_limits import parse_listing_date
from app.services import instrument_sync, kline_periods

logger = logging.getLogger(__name__)


def _trading_days(start: date, end: date, *, local_only: bool = False) -> list[date]:
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    unknown = [day for day in days if market_time.published_trading_day(day) is None]
    evidence = None
    if unknown:
        from app.services.data_integrity import _trading_calendar, cached_trading_calendar
        evidence = cached_trading_calendar() if local_only else _trading_calendar()
        if not evidence or min(unknown) < min(evidence) or max(unknown) > max(evidence):
            raise ValueError("选定区间缺少可靠交易日历, 无法判断遗漏数据; 请缩小范围或配置交易日历数据源")
    return [
        day for day in days
        if market_time.published_trading_day(day) is True
        or (market_time.published_trading_day(day) is None and day in evidence)
    ]


def _keys(repo, period, day) -> pl.DataFrame:
    column = "date" if period in ("1d", "1w") else "datetime"
    paths = list((repo.store.data_dir / kline_periods.TABLES[period] / f"date={day}").glob("*.parquet"))
    if not paths:
        return pl.DataFrame(schema={"symbol": pl.String, column: pl.Date if column == "date" else pl.Datetime("us")})
    return pl.scan_parquet(paths).select("symbol", column).drop_nulls().unique().collect()


def _expected(symbols, listing_dates, period, day, last_day, now):
    eligible = [symbol for symbol in symbols if listing_dates.get(symbol) is None or listing_dates[symbol] <= last_day]
    column = "date" if period in ("1d", "1w") else "datetime"
    if column == "date":
        stamps = pl.DataFrame({column: [day]}, schema={column: pl.Date})
    else:
        step = int(period[:-1])
        stamps = pl.DataFrame({
            column: [stamp for base in (570, 780) for i in range(step, 121, step)
                     if (stamp := datetime.combine(day, time.min) + timedelta(minutes=base + i)) <= now],
        }, schema={column: pl.Datetime("us")})
    return pl.DataFrame({"symbol": eligible}, schema={"symbol": pl.String}).join(stamps, how="cross")


def _requests(gaps, period, day, last_day, now):
    """仅合并同一证券连续的缺口; 午休和已有记录不会被请求跨过。"""
    if period in ("1d", "1w"):
        yield gaps["symbol"].unique().sort().to_list(), datetime.combine(day, time.min), min(datetime.combine(last_day, time.max), now)
        return
    grouped = defaultdict(list)
    step = timedelta(minutes=int(period[:-1]))
    for symbol_frame in gaps.sort("symbol", "datetime").partition_by("symbol", maintain_order=True):
        stamps = symbol_frame["datetime"].to_list()
        start = previous = stamps[0]
        for stamp in stamps[1:]:
            if stamp - previous != step:
                grouped[(start, previous)].append(symbol_frame["symbol"][0])
                start = stamp
            previous = stamp
        grouped[(start, previous)].append(symbol_frame["symbol"][0])
    for (start, end), symbols in sorted(grouped.items()):
        yield sorted(symbols), start, end


def _local_minute_period(repo, period, day, symbols):
    paths = list((repo.store.data_dir / "kline_minute" / f"date={day}").glob("*.parquet"))
    if not paths:
        return pl.DataFrame()
    frame = pl.scan_parquet(paths).filter(pl.col("symbol").is_in(symbols)).collect()
    return kline_periods.aggregate_minute_period(frame, period, complete_only=True)


def _local_weekly_period(repo, start, end, symbols):
    frame = kline_periods._local_daily_frame(repo, start, end, symbols)
    return kline_periods.aggregate_weekly(frame)


def sync_missing(repo, period: str, start: date, end: date, *, on_progress=None) -> dict:
    from app.services.minute_adjust import minute_basis_is_raw
    from app.services.pipeline_jobs import JobCancelledError
    from app.services.realtime_guard import pause_quotes_for_write

    now = market_time.as_cn_datetime(market_time.cn_now()).replace(tzinfo=None, second=0, microsecond=0)
    if start > end:
        return {"error": "开始日期不得晚于结束日期"}
    snapshot = instrument_sync.load_current_stock_pool(repo.store.data_dir, require_complete=True)
    symbols = list(snapshot.symbols)
    instruments = repo.get_instruments()
    listing_dates = {}
    if "listing_date" in instruments.columns:
        listing_dates = {symbol: parse_listing_date(value) for symbol, value in instruments.select("symbol", "listing_date").iter_rows()}
    calendar_start = start - timedelta(days=start.weekday()) if period == "1w" else start
    local = period in kline_periods.LOCAL_DERIVED_PERIODS
    try:
        days = _trading_days(calendar_start, end, local_only=local)
    except ValueError as exc:
        return {"error": str(exc)}
    if period == "1d":
        days = [day for day in days if day < now.date() or now.time() >= time(15)]
    elif period == "1w":
        days = [day for day in days if day < now.date() or now.time() >= time(9, 31)]
    units = {}
    for day in days:
        label = day - timedelta(days=day.weekday()) if period == "1w" else day
        units[label] = day
    raw_minute = minute_basis_is_raw(repo.store.data_dir) or not any((repo.store.data_dir / "kline_minute").rglob("*.parquet"))
    result = {
        "period": period, "mode": "missing", "rows": 0, "missing_before": 0, "remaining": 0,
        "requests": 0, "universe_size": len(symbols), "start": start.isoformat(), "end": end.isoformat(),
        "source": "1d" if period in kline_periods.LOCAL_DAILY_PERIODS else ("1m" if local else None),
        "price_basis": ("raw" if raw_minute else "legacy_1m") if period == "1m" or period in kline_periods.LOCAL_MINUTE_PERIODS else "raw",
    }
    provider = None
    affected = set()
    failures = 0
    column = "date" if period in ("1d", "1w") else "datetime"
    keys = ["symbol", column]

    def emit(message, completed):
        if on_progress:
            fraction = completed / max(1, len(units))
            on_progress(f"sync_{period}", 5 + int(fraction * 85), message, stage_pct=int(fraction * 100))

    for completed, (day, last_day) in enumerate(units.items()):
        emit(f"{kline_periods.LABELS[period]}: 检查 {day} 的遗漏数据", completed)
        existing = _keys(repo, period, day)
        for offset in range(0, len(symbols), 100):
            emit(f"{day}: 检查标的 {offset}/{len(symbols)}, 已补{result['rows']}行", completed)
            expected = _expected(symbols[offset:offset + 100], listing_dates, period, day, last_day, now)
            gaps = expected.join(existing, on=keys, how="anti")
            result["missing_before"] += gaps.height
            if gaps.is_empty():
                continue
            if local:
                symbols_for_gap = gaps["symbol"].unique().to_list()
                frame = (
                    _local_weekly_period(repo, day, last_day, symbols_for_gap)
                    if period in kline_periods.LOCAL_DAILY_PERIODS
                    else _local_minute_period(repo, period, day, symbols_for_gap)
                )
                emit(f"{day}: 保存遗漏的{kline_periods.LABELS[period]}", completed)
                if not frame.is_empty():
                    frame = frame.join(gaps, on=keys, how="inner")
                    with pause_quotes_for_write():
                        result["rows"] += kline_periods.persist_period(repo, period, frame, only_missing=True)
            else:
                if provider is None:
                    provider, result["source"] = kline_periods._period_provider(period)
                for batch, lo, hi in _requests(gaps, period, day, last_day, now):
                    emit(f"{day}: 获取 {len(batch)} 只证券的遗漏区间 {lo.time()}~{hi.time()}", completed)
                    result["requests"] += 1
                    try:
                        frame = kline_periods.fetch_period(provider, period, batch, lo, hi, minute_adjust="none" if raw_minute else "forward")
                    except JobCancelledError:
                        raise
                    except Exception:  # 分块失败保留已补数据, 终态明确失败。
                        failures += 1
                        logger.warning("missing Kline fetch failed: period=%s day=%s", period, day, exc_info=True)
                        continue
                    emit(f"{day}: 保存遗漏数据", completed)  # 获取后再次检查协作取消。
                    if not frame.is_empty():
                        frame = frame.join(gaps, on=keys, how="inner")
                        with pause_quotes_for_write():
                            written = kline_periods.persist_period(repo, period, frame, only_missing=True)
                        result["rows"] += written
                        if period == "1d" and written:
                            affected.update(frame["symbol"].to_list())
        # 并发同步可能已经填入缺口, 终态以重新读取的持久数据为准。
        current = _keys(repo, period, day)
        for offset in range(0, len(symbols), 100):
            expected = _expected(symbols[offset:offset + 100], listing_dates, period, day, last_day, now)
            result["remaining"] += expected.join(current, on=keys, how="anti").height
        emit(f"{day}: 已补{result['rows']}行, 剩余{result['remaining']}行", completed + 1)
    if affected:
        from app.indicators.pipeline import run_pipeline
        from app.jobs.daily_pipeline import _refresh_single_view
        from app.services.public_query_cache import invalidate_data_cache
        emit("重算已补日K证券的复权与指标", len(units))
        callback = (lambda done, total: emit(f"重算已完成 {done}/{total} 批", len(units))) if on_progress else None
        try:
            result["enriched_rows"] = run_pipeline(data_dir=repo.store.data_dir, symbols=sorted(affected), on_batch_done=callback)
        finally:
            _refresh_single_view(repo, "kline_enriched")
            invalidate_data_cache("daily")
    if result["remaining"] or failures:
        reason = (
            "基础日K不完整, 请先获取遗漏的日K后重试"
            if period in kline_periods.LOCAL_DAILY_PERIODS
            else "基础1分K不完整, 请先获取遗漏的1分K后重试"
            if local else "数据源未返回部分记录(可能停牌、无数据或请求失败)"
        )
        result["error"] = f"已补{result['rows']}行, 仍有{result['remaining']}行未补齐; {reason}"
    elif result["missing_before"] == 0:
        result["message"] = "选定区间没有遗漏数据, 无需获取"
    return result
