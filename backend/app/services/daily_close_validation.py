"""Completed original-price references, independent of quote receipt clocks."""
from datetime import date, datetime, time

import polars as pl

from app.market_time import CN_TZ

CLOCK_COLUMNS = ("quote_ts", "quote_time_basis")
PRICE_TOLERANCE = 0.005


def _reject(day: date, frame: pl.DataFrame, reason: str, rejected: dict | None = None) -> None:
    if rejected is not None and "symbol" in frame.columns and not frame["symbol"].is_null().any():
        for symbol in frame["symbol"].unique():
            rejected[str(symbol)] = f"{day} {symbol} 收盘校验失败: {reason}"
        return
    row = frame.row(0, named=True) if not frame.is_empty() else {}
    price = row.get("raw_close", row.get("close"))
    raise ValueError(f"{day} {row.get('symbol', '*')} 收盘校验失败: {reason}; price={price}")


def require_final_daily_rows(
    frame: pl.DataFrame, day: date, *, isolate_symbols: bool = False,
) -> dict | None:
    """Null clocks retain the official batch contract; receipt clocks never finalize."""
    if frame.is_empty() or not {"symbol", "date"} <= set(frame.columns):
        _reject(day, frame, "缺少准确交易日日线")
    price = "raw_close" if "raw_close" in frame.columns else "close"
    if price not in frame.columns:
        _reject(day, frame, "缺少原始收盘价")
    rejected = {} if isolate_symbols else None
    numeric_price = pl.col(price).cast(pl.Float64, strict=False)
    bad = frame.filter(
        pl.col("symbol").is_null() | (pl.col("date") != day).fill_null(True)
        | ~numeric_price.is_finite().fill_null(False)
        | (numeric_price <= 0).fill_null(True)
    )
    if not bad.is_empty():
        _reject(day, bad, "交易日或原始收盘价无效", rejected)
    duplicates = frame.group_by("symbol").len().filter(pl.col("len") != 1)
    if not duplicates.is_empty():
        _reject(day, duplicates, "重复日线", rejected)
    stamp = pl.col("quote_ts").cast(pl.Int64, strict=False) if "quote_ts" in frame.columns else pl.lit(None, dtype=pl.Int64)
    basis = pl.col("quote_time_basis") if "quote_time_basis" in frame.columns else pl.lit(None, dtype=pl.String)
    cutoff = int(datetime.combine(day, time(15), tzinfo=CN_TZ).timestamp() * 1000)
    end = int(datetime.combine(day, time.max, tzinfo=CN_TZ).timestamp() * 1000)
    batch = basis.is_null() & stamp.is_null()
    snapshot = (basis == "source_snapshot") & stamp.is_between(cutoff, end)
    bad = frame.filter(~(batch | snapshot).fill_null(False))
    if not bad.is_empty():
        _reject(day, bad, "缺少当日15:00后源端快照时间; 接收/事件时间不证明定版", rejected)
    return rejected


def validate_daily_close(
    repo, day: date, rows: pl.DataFrame | None = None, *, isolate_symbols: bool = False,
) -> dict | None:
    """Read one raw partition and its unique 15:00 bar, never the live cache."""
    try:
        raw, closing = repo.get_daily_close_inputs(day)
    except (OSError, pl.exceptions.PolarsError, AttributeError) as exc:
        raise ValueError(f"{day}收盘定版证据不可读: {exc}") from exc
    rejected = require_final_daily_rows(raw, day, isolate_symbols=isolate_symbols)
    if rejected:
        raw = raw.filter(~pl.col("symbol").is_in(list(rejected)))
    from app.instrument_suspension import STATUS_COLUMN, dated_status

    getter = getattr(repo, "get_suspension_history", None)
    status = dated_status(getter(), day, observed_at=datetime.combine(day, time(15))) if callable(getter) else pl.DataFrame()
    suspended = (
        status.filter(pl.col(STATUS_COLUMN)).get_column("symbol").to_list()
        if not status.is_empty() else []
    )
    if closing.is_empty():
        closing = pl.DataFrame(schema={"symbol": pl.String, "close": pl.Float64})
    raw = raw.with_columns(pl.col("close").cast(pl.Float64, strict=False))
    closing = closing.with_columns(pl.col("close").cast(pl.Float64, strict=False))
    counts = closing.group_by("symbol").agg(
        pl.len().alias("_count"), pl.col("close").first().alias("_auction_close"),
    )
    checked = raw.join(counts, on="symbol", how="left")
    bad = checked.filter(
        (pl.col("_count") > 1).fill_null(False)
        | (~pl.col("symbol").is_in(suspended) & (
            (pl.col("_count") != 1).fill_null(True)
            | ~pl.col("_auction_close").is_finite().fill_null(False)
            | (pl.col("_auction_close") <= 0).fill_null(True)
        ))
        | ((pl.col("close") - pl.col("_auction_close")).abs() > PRICE_TOLERANCE).fill_null(False)
    )
    if not bad.is_empty():
        _reject(day, bad, "原始日线与唯一有效15:00收盘分钟不一致或分钟缺失", rejected)
    if rows is not None:
        invalid = require_final_daily_rows(rows, day, isolate_symbols=isolate_symbols)
        if rejected is not None:
            rejected.update(invalid or {})
            rows = rows.filter(~pl.col("symbol").is_in(list(rejected)))
        price = "raw_close" if "raw_close" in rows.columns else "close"
        reference = rows.select("symbol", pl.col(price).cast(pl.Float64, strict=False).alias("raw_close")).join(
            raw.select("symbol", pl.col("close").alias("_official_close")),
            on="symbol", how="left",
        )
        bad = reference.filter(
            pl.col("_official_close").is_null()
            | ((pl.col("raw_close") - pl.col("_official_close")).abs() > PRICE_TOLERANCE)
        )
        if not bad.is_empty():
            _reject(day, bad, "加工/策略昨收与原始日线不一致", rejected)
    return rejected


def validate_completed_pipeline(
    repo, start: date, end: date, *, now: datetime, minute_start: date | None = None,
) -> None:
    """Initial history retains batch finality outside its configured minute window."""
    from app.market_time import published_trading_day

    now = now.astimezone(CN_TZ) if now.tzinfo else now.replace(tzinfo=CN_TZ)
    from datetime import timedelta

    if minute_start is None:
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        # A source can return less history for recently listed instruments. Check
        # every received partition; consumers still require exact T-1 auction proof.
        days = sorted(
            date.fromisoformat(part.name[5:])
            for part in (repo.store.data_dir / "kline_daily").glob("date=*")
            if start.isoformat() <= part.name[5:] <= end.isoformat()
            and any(part.glob("*.parquet"))
        )
    for day in days:
        if published_trading_day(day) and (
            day < now.date() or (day == now.date() and now.time() >= time(15))
        ):
            if minute_start is not None and day < minute_start:
                raw, _ = repo.get_daily_close_inputs(day)
                require_final_daily_rows(raw, day)
            else:
                validate_daily_close(repo, day)
