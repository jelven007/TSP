"""六周期 K 线配置、取数和落盘; 分钟/周K均由本地基础K线派生。"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

import polars as pl

from app.market_time import CN_TZ, TRADING_DAY_COLLECTION_WINDOWS, cn_now
from app.services import preferences

TABLES = {
    "1m": "kline_minute", "5m": "kline_5minute", "10m": "kline_10minute",
    "15m": "kline_15minute", "1d": "kline_daily", "1w": "kline_weekly",
}
LABELS = {"1m": "1分K", "5m": "5分K", "10m": "10分K", "15m": "15分K", "1d": "日K", "1w": "周K"}
LOCAL_MINUTE_PERIODS = ("5m", "10m", "15m")
LOCAL_DAILY_PERIODS = ("1w",)
LOCAL_DERIVED_PERIODS = (*LOCAL_MINUTE_PERIODS, *LOCAL_DAILY_PERIODS)
DEFAULT_WINDOWS = [list(window) for window in TRADING_DAY_COLLECTION_WINDOWS]
_VALUES = ("open", "high", "low", "close", "volume", "amount")


def get_configs(data_dir=None) -> dict:
    prefs = preferences.load(data_dir)
    result = {}
    for period in TABLES:
        default = {
            "enabled": True,
            "interval_seconds": {"1m": 60, "5m": 300, "10m": 600, "15m": 900}.get(period, 60),
            "windows": [w[:] for w in DEFAULT_WINDOWS], "time": "16:00",
        }
        if period == "1m":
            default["enabled"] = prefs.get("minute_refresh_enabled", True)
            default["interval_seconds"] = prefs.get("minute_refresh_interval", 60)
        if period == "1d":
            sched = prefs.get("pipeline_schedule", {"hour": 16, "minute": 0})
            default["time"] = f'{sched.get("hour", 16):02d}:{sched.get("minute", 0):02d}'
        saved = prefs.get(f"kline_sync_{period}", {})
        result[period] = {**default, **(saved if isinstance(saved, dict) else {})}
        result[period]["windows"] = [window[:] for window in DEFAULT_WINDOWS]
        if period == "1d":
            result[period]["time"] = default["time"]
    return result


def _parse_time(value: str) -> time:
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
        if parsed.strftime("%H:%M") != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError) as exc:
        raise ValueError("时间须为 HH:MM") from exc


def save_config(period: str, config: dict, data_dir=None) -> dict:
    if period not in TABLES:
        raise ValueError("不支持的K线周期")
    merged = {**get_configs(data_dir)[period], **config}
    if type(merged["enabled"]) is not bool:
        raise ValueError("同步开关须为布尔值")
    interval = merged["interval_seconds"]
    if type(interval) is not int or not 1 <= interval <= 86400:
        raise ValueError("周期间隔须为1~86400秒")
    windows = merged["windows"]
    if not isinstance(windows, list) or len(windows) != 2:
        raise ValueError("须设置两个同步时段")
    previous = None
    for window in windows:
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("同步时段须包括开始和结束时间")
        start, end = map(_parse_time, window)
        if start >= end or (previous is not None and start <= previous):
            raise ValueError("同步时段须按时间排序且不能重叠")
        previous = end
    merged["windows"] = [window[:] for window in DEFAULT_WINDOWS]
    at = _parse_time(merged["time"])
    if period in ("1d", "1w") and at < time(15, 35):
        raise ValueError("盘后同步时间不得早于15:35")
    updates = {f"kline_sync_{period}": merged}
    if period == "1m":
        updates.update(minute_refresh_enabled=merged["enabled"], minute_refresh_interval=interval)
    if period == "1d":
        updates["pipeline_schedule"] = {"hour": at.hour, "minute": at.minute}
    preferences.save(updates, data_dir)
    return merged


def in_sync_window(config: dict, now: datetime | None = None, *, trading_day: bool | None = None) -> bool:
    now = now or cn_now()
    if now.tzinfo is not None:
        now = now.astimezone(CN_TZ)
    if trading_day is None:
        from app.services.trading_day import is_trading_day
        trading_day = is_trading_day(now)
    return config["enabled"] and trading_day is True and any(
        _parse_time(start) <= now.time().replace(tzinfo=None) < _parse_time(end)
        for start, end in config["windows"]
    )


def history_start(anchor: date, value: int, unit: str) -> date:
    if type(value) is not int or value < 1 or unit not in ("month", "year"):
        raise ValueError("历史范围须为正整数月或年")
    if value > (120 if unit == "month" else 10):
        raise ValueError("单次历史扩展最多10年")
    months = value * (12 if unit == "year" else 1)
    ordinal = anchor.year * 12 + anchor.month - 1 - months
    year, month0 = divmod(ordinal, 12)
    return date(year, month0 + 1, min(anchor.day, calendar.monthrange(year, month0 + 1)[1]))


def aggregate_minute_period(frame: pl.DataFrame, period: str, *, complete_only: bool = False) -> pl.DataFrame:
    """收盘标签按 09:30/13:00 起算, 午休与跨日不混桶, 未知值保留。"""
    if period not in LOCAL_MINUTE_PERIODS:
        raise ValueError("不支持的本地聚合周期")
    step = int(period[:-1])
    from app.services.kline_sync import _normalize_minute
    frame = _normalize_minute(frame)
    if frame.is_empty():
        return frame
    for column in _VALUES:
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    minute = pl.col("datetime").dt.hour().cast(pl.Int32) * 60 + pl.col("datetime").dt.minute()
    frame = frame.filter(
        ((minute > 570 if complete_only else minute >= 570) & (minute <= 690))
        | ((minute > 780 if complete_only else minute >= 780) & (minute <= 900)),
    )
    if frame.is_empty():
        return frame
    base = pl.when(minute <= 690).then(570).otherwise(780)
    bucket = base + (((minute - base + step - 1) // step).clip(lower_bound=1) * step)
    frame = frame.with_columns(
        (pl.col("datetime").dt.truncate("1d") + pl.duration(minutes=bucket)).alias("_bucket"),
        *[pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in _VALUES],
    ).sort("symbol", "datetime").unique(["symbol", "datetime"], keep="last", maintain_order=True)
    def finite(c):
        return pl.col(c).filter(pl.col(c).is_finite())
    result = (
        frame.group_by("symbol", "_bucket", maintain_order=True).agg(
            pl.len().alias("_minutes"),
            pl.col("open").first(), finite("high").max().alias("high"),
            finite("low").min().alias("low"), pl.col("close").last(),
            *[
                pl.when(finite(c).count() > 0).then(finite(c).sum()).otherwise(None).alias(c)
                for c in ("volume", "amount")
            ],
        ).rename({"_bucket": "datetime"}).sort("symbol", "datetime")
    )
    # 遗漏补齐只接受完整基础桶; 常规同步仍保留盘中尚未完整的桶。
    if complete_only:
        result = result.filter(pl.col("_minutes") == step)
    return result.drop("_minutes")


def aggregate_ten_minute(frame: pl.DataFrame) -> pl.DataFrame:
    """保留原10分钟聚合调用契约。"""
    return aggregate_minute_period(frame, "10m")


def aggregate_weekly(frame: pl.DataFrame) -> pl.DataFrame:
    """将本地日K按周一标签聚合为周K, 未知值保持为空。"""
    from app.services.kline_sync import _normalize_daily

    frame = _normalize_daily(frame)
    if frame.is_empty():
        return frame
    for column in _VALUES:
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    frame = frame.with_columns(
        (pl.col("date") - pl.duration(days=pl.col("date").dt.weekday() - 1)).cast(pl.Date).alias("_week"),
        *[pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in _VALUES],
    ).sort("symbol", "date").unique(["symbol", "date"], keep="last", maintain_order=True)

    def finite(column):
        return pl.col(column).filter(pl.col(column).is_finite())

    return (
        frame.group_by("symbol", "_week", maintain_order=True).agg(
            finite("open").first().alias("open"),
            finite("high").max().alias("high"),
            finite("low").min().alias("low"),
            finite("close").last().alias("close"),
            *[
                pl.when(finite(column).count() > 0).then(finite(column).sum()).otherwise(None).alias(column)
                for column in ("volume", "amount")
            ],
        ).rename({"_week": "date"}).sort("symbol", "date")
    )


def _local_daily_frame(repo, start: date, end: date, symbols: list[str]) -> pl.DataFrame:
    paths = []
    day = start
    while day <= end:
        paths.extend((repo.store.data_dir / TABLES["1d"] / f"date={day}").glob("*.parquet"))
        day += timedelta(days=1)
    if not paths:
        return pl.DataFrame()
    return pl.scan_parquet(paths).filter(
        pl.col("symbol").is_in(symbols) & pl.col("date").is_between(start, end),
    ).collect()


def fetch_period(provider, period: str, symbols: list[str], start: datetime, end: datetime, *, minute_adjust: str = "none") -> pl.DataFrame:
    """统一 provider 入口; 旧插件只允许已声明的1m/1d, 不猜测其他周期。"""
    if period in LOCAL_DERIVED_PERIODS or period not in TABLES:
        raise ValueError("该周期不能从远程获取")
    method = getattr(provider, "get_klines", None)
    if callable(method):
        kwargs = {"adjust": minute_adjust} if period == "1m" and getattr(provider, "name", "") == "tickflow" else {}
        raw = method(symbols, start, end, asset_type="stock", freq=period, **kwargs)
    elif period == "1d":
        raw = provider.get_daily(symbols, start, end, asset_type="stock")
    elif period == "1m":
        raw = provider.get_minute(symbols, start, end, asset_type="stock", freq=period)
    else:
        raise ValueError("当前数据源未声明该K线周期, 请更新数据源接口")
    from app.services.kline_sync import _normalize_daily, _normalize_minute
    frame = (_normalize_daily if period in ("1d", "1w") else _normalize_minute)(raw)
    if frame.is_empty():
        return frame
    if period == "1w":
        frame = frame.with_columns(
            (pl.col("date") - pl.duration(days=pl.col("date").dt.weekday() - 1)).cast(pl.Date).alias("date"),
        )
    time_column = "date" if period in ("1d", "1w") else "datetime"
    lo, hi = (start.date(), end.date()) if time_column == "date" else (start, end)
    return frame.filter(pl.col("symbol").is_in(symbols) & pl.col(time_column).is_between(lo, hi))


def persist_period(repo, period: str, frame: pl.DataFrame, *, only_missing: bool = False) -> int:
    from app.services.kline_sync import (
        _atomic_write_parquet,
        _minute_partition_lock,
        _write_minute_partition,
    )
    if period not in TABLES:
        raise ValueError("不支持的K线周期")
    if frame.is_empty():
        return 0
    if period == "1m":
        from app.services.minute_adjust import mark_minute_basis_raw, minute_basis_is_raw
        if not minute_basis_is_raw(repo.store.data_dir) and not any((repo.store.data_dir / "kline_minute").rglob("*.parquet")):
            mark_minute_basis_raw(repo.store.data_dir)
    if period == "1w":
        # 上游当前周标签随交易日变化, 统一周一使周内增量替换同一根周K。
        frame = frame.with_columns(
            (pl.col("date") - pl.duration(days=pl.col("date").dt.weekday() - 1)).cast(pl.Date).alias("date"),
        )
    directory = repo.store.data_dir / TABLES[period]
    keys = ["symbol", "date" if period in ("1d", "1w") else "datetime"]
    written = 0
    if period in ("1d", "1w"):
        for day_frame in frame.partition_by("date"):
            out = directory / f'date={day_frame["date"][0]}' / "part.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            with _minute_partition_lock(out):
                existing = pl.read_parquet(out) if out.exists() else None
                day_frame = day_frame.unique(keys, keep="last")
                if only_missing and existing is not None:
                    day_frame = day_frame.join(existing.select(keys), on=keys, how="anti")
                if day_frame.is_empty():
                    continue
                written += day_frame.height
                merged = pl.concat([existing, day_frame], how="diagonal_relaxed") if existing is not None else day_frame
                merged = merged.unique(["symbol", "date"], keep="last").sort("symbol", "date")
                _atomic_write_parquet(merged, out)
    else:
        # 旧同步可先持仓库锁; 此处只复用分区锁, 避免反序获取全局锁。
        # 公共发布复核前后文件签名, 原子替换保证读者始终读取完整文件。
        written = _write_minute_partition(frame, directory, only_missing=only_missing)
    refresh_period(repo, period)
    return written if only_missing else frame.unique(keys).height


def refresh_period(repo, period: str) -> None:
    from app.jobs.daily_pipeline import _refresh_single_view
    from app.services.public_query_cache import invalidate_data_cache, invalidate_storage_cache
    _refresh_single_view(repo, TABLES[period])
    invalidate_data_cache({"1m": "minute", "1d": "daily", "1w": "weekly"}.get(period, period))
    invalidate_storage_cache()


def period_bounds(repo, period: str) -> tuple[date | None, date | None]:
    import pyarrow.parquet as pq
    days = set()
    for path in (repo.store.data_dir / TABLES[period]).glob("date=*/*.parquet"):
        try:
            if pq.ParquetFile(path).metadata.num_rows:
                days.add(date.fromisoformat(path.parent.name.removeprefix("date=")))
        except (OSError, ValueError):
            continue
    return (min(days), max(days)) if days else (None, None)

def _period_provider(period: str):
    from app.data_providers.custom import provider_has_dataset
    from app.data_providers.registry import get_provider

    dataset = "daily" if period in ("1d", "1w") else "minute"
    source = preferences.get_daily_data_provider() if dataset == "daily" else preferences.get_minute_data_provider()
    provider = get_provider(source)
    if provider is None or not (
        getattr(getattr(provider, "capabilities", None), dataset, False)
        or provider_has_dataset(source, dataset)
    ):
        raise ValueError("当前K线数据源不可用")
    return provider, source


def query_period(repo, period: str, symbol: str, start: datetime, end: datetime, *, live: bool = False) -> tuple[pl.DataFrame, str]:
    """派生分钟优先计算本地1m, 周K无本地表时由日K聚合。"""
    if period not in ("1m", "5m", "10m", "15m", "1w") or start > end:
        raise ValueError("K线周期或日期范围无效")
    if repo.resolve_asset_type(symbol) != "stock":
        raise ValueError("该周期当前仅支持个股")
    local = period in LOCAL_MINUTE_PERIODS
    if local:
        # 先读整日基础数据再按聚合标签裁剪, 起点在桶内也不会丢失开盘价和量额。
        frame = repo.get_minute_range([symbol], start.date(), end.date(), asset_type="stock")
        if not frame.is_empty():
            frame = aggregate_minute_period(frame, period).filter(pl.col("datetime").is_between(start, end))
            if not frame.is_empty():
                return frame, "local"
    if period == "1m" and not live:
        frame = repo.get_minute_range([symbol], start.date(), end.date(), asset_type="stock")
        if not frame.is_empty():
            return frame.filter(pl.col("datetime").is_between(start, end)).sort("datetime"), "local"
    weekly = period == "1w"
    column = "date" if weekly else "datetime"
    lo = start.date() - timedelta(days=start.weekday()) if weekly else start
    hi = end.date() if weekly else end
    paths = []
    for directory in (repo.store.data_dir / TABLES[period]).glob("date=*"):
        day = date.fromisoformat(directory.name.removeprefix("date="))
        if (lo if weekly else lo.date()) <= day <= (hi if weekly else hi.date()):
            paths.extend(directory.glob("*.parquet"))
    if paths and (not live or local) and period != "1m":
        frame = pl.scan_parquet(paths).filter(
            (pl.col("symbol") == symbol) & pl.col(column).is_between(lo, hi),
        ).sort(column).collect()
        if not frame.is_empty():
            return frame, "local"
    if weekly:
        frame = aggregate_weekly(_local_daily_frame(repo, lo, end.date(), [symbol]))
        if not frame.is_empty():
            return frame.filter(pl.col("date").is_between(lo, hi)), "local"
        return pl.DataFrame(), "none"
    if local:
        return pl.DataFrame(), "none"
    provider, _ = _period_provider(period)
    fetch_start = datetime.combine(lo, time.min) if weekly else start
    frame = fetch_period(provider, period, [symbol], fetch_start, end)
    if period == "1m" and not frame.is_empty():
        from app.services.minute_adjust import apply_minute_adjustment
        frame = apply_minute_adjustment(frame, repo.store.data_dir, "stock")
    if weekly and not frame.is_empty():
        frame = frame.with_columns(
            (pl.col("date") - pl.duration(days=pl.col("date").dt.weekday() - 1)).cast(pl.Date).alias("date"),
        )
    return frame, "live" if not frame.is_empty() else "none"


def sync_period(repo, period: str, start: datetime, end: datetime, *, symbols: list[str] | None = None, on_progress=None) -> dict:
    if period not in TABLES:
        raise ValueError("不支持的K线周期")
    if start > end:
        raise ValueError("开始时间不得晚于结束时间")
    from app.services.minute_adjust import minute_basis_is_raw
    raw_minute = minute_basis_is_raw(repo.store.data_dir) or not any((repo.store.data_dir / "kline_minute").rglob("*.parquet"))
    if symbols is None:
        symbols = repo.get_instruments().get_column("symbol").to_list()
    provider = None
    source = "1d" if period in LOCAL_DAILY_PERIODS else "1m"
    local = period in LOCAL_DERIVED_PERIODS
    if not local:
        provider, source = _period_provider(period)
    rows = 0
    # 有界日期段和标的块; 历史分钟数据不一次性加载全市场多年数据。
    days_per_chunk = 5 if period.endswith("m") else 364
    cursor = start.date() - timedelta(days=start.weekday()) if period == "1w" else start.date()
    segments = max(1, (end.date() - cursor).days // days_per_chunk + 1)
    batches_per_segment = 1 if local else max(1, (len(symbols) + 99) // 100)
    total_batches = segments * batches_per_segment
    completed_batches = 0

    def emit(message):
        if on_progress:
            fraction = completed_batches / total_batches
            on_progress(
                f"sync_{period}", 10 + int(fraction * 85), message,
                stage_pct=int(fraction * 100),
            )

    while cursor <= end.date():
        emit(f"{LABELS[period]}: 准备{'本地计算' if local else '获取'} {cursor}")
        segment_end = min(end.date(), cursor + timedelta(days=days_per_chunk - 1))
        lo = max(start, datetime.combine(cursor, time.min))
        hi = min(end, datetime.combine(segment_end, time.max))
        if local:
            if period in LOCAL_DAILY_PERIODS:
                frame = aggregate_weekly(_local_daily_frame(repo, cursor, segment_end, symbols))
            else:
                paths = []
                day = cursor
                while day <= segment_end:
                    paths.extend((repo.store.data_dir / "kline_minute" / f"date={day}").glob("*.parquet"))
                    day += timedelta(days=1)
                frame = pl.DataFrame()
                if paths:
                    frame = pl.scan_parquet(paths).filter(pl.col("datetime").is_between(lo, hi))
                    frame = frame.filter(pl.col("symbol").is_in(symbols))
                    frame = aggregate_minute_period(frame.collect(), period)
            if not frame.is_empty():
                emit(f"{LABELS[period]}: 正在保存本地计算结果")
                rows += persist_period(repo, period, frame)
            completed_batches += 1
        else:
            for offset in range(0, len(symbols), 100):
                emit(f"{LABELS[period]}: {cursor}, 标的 {offset}/{len(symbols)}")
                frame = fetch_period(provider, period, symbols[offset:offset + 100], lo, hi, minute_adjust="none" if raw_minute else "forward")
                emit(f"{LABELS[period]}: 正在保存 {min(offset + 100, len(symbols))}/{len(symbols)}")
                rows += persist_period(repo, period, frame)
                completed_batches += 1
                emit(f"{LABELS[period]}: 已完成 {completed_batches}/{total_batches} 批, 已写入{rows}行")
            if not symbols:
                completed_batches += 1
        emit(f"{LABELS[period]}: {cursor}~{segment_end}, 已写入{rows}行")
        cursor = segment_end + timedelta(days=1)
    result = {"period": period, "rows": rows, "universe_size": len(symbols), "source": source, "start": start.isoformat(), "end": end.isoformat(),
              "price_basis": ("raw" if raw_minute else "legacy_1m") if period == "1m" or local else "raw"}
    if local and rows == 0:
        result["error"] = (
            "选定区间没有可计算的本地日K, 请先补齐日K后重试"
            if period in LOCAL_DAILY_PERIODS else "选定区间没有可计算的本地1分K, 请先补齐1分K后重试"
        )
    return result


_last_success: dict[str, datetime] = {}


def scheduled_periods(repo) -> None:
    """每30秒检查, 分钟K按交易日运行, 周K周六由本地日K聚合。"""
    from app.jobs.daily_pipeline import _run_tracked
    from app.services.trading_day import is_trading_day
    if not preferences.get_data_sync_auto_enabled():
        return
    now = cn_now().replace(tzinfo=None)
    configs = get_configs()
    weekly = configs["1w"]
    last = _last_success.get("1w")
    if (
        weekly["enabled"]
        and now.weekday() == 5
        and now.time() >= _parse_time(weekly["time"])
        and not (last and last.date() == now.date())
    ):
        start = datetime.combine(now.date() - timedelta(days=now.weekday()), time.min)
        if _run_tracked(
            lambda on_progress=None, lo=start: sync_period(repo, "1w", lo, now, on_progress=on_progress),
            "kline_1w",
        ):
            _last_success["1w"] = cn_now().replace(tzinfo=None)
    verdict = is_trading_day(now)
    if verdict is not True:
        return
    for period in ("5m", "10m", "15m"):
        cfg = configs[period]
        if not cfg["enabled"]:
            continue
        last = _last_success.get(period)
        if not in_sync_window(cfg, now, trading_day=True) or (last and (now - last).total_seconds() < cfg["interval_seconds"]):
            continue
        start = datetime.combine(now.date(), time.min)
        if _run_tracked(lambda on_progress=None, p=period, lo=start: sync_period(repo, p, lo, now, on_progress=on_progress), f"kline_{period}"):
            _last_success[period] = cn_now().replace(tzinfo=None)
