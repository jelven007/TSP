"""Shared minute-return basis and price-quality checks for sector analytics."""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from pathlib import Path

import polars as pl

from app.data_providers.instrument_status import is_current_a_share
from app.instrument_history import attach_instrument_history, load_instrument_history
from app.market_time import published_trading_days
from app.price_limits import (
    is_no_limit_day,
    no_limit_window_days,
    no_limit_window_margin_days,
    polars_limit_price,
    price_limit_pct,
)
from app.services.minute_adjust import compute_ratio_pairs, minute_basis_is_raw

logger = logging.getLogger(__name__)


def _previous_close(data_dir: Path, target: date, raw: bool) -> pl.DataFrame:
    """Project previous raw close onto the target day's raw price basis.

    Future factors cancel in prev_ratio / target_ratio. Legacy minute partitions
    lack a proven price basis; when adjustment matters, use their first price
    instead of mixing forward-adjusted minutes and raw daily closes.
    """
    schema = {"symbol": pl.String, "prev_close": pl.Float64}
    paths = sorted((data_dir / "kline_daily").glob("date=*/part.parquet"))
    paths = [p for p in paths if p.parent.name[5:] < target.isoformat()]
    if not paths:
        return pl.DataFrame(schema=schema)
    previous = date.fromisoformat(paths[-1].parent.name[5:])
    calendars = [published_trading_days(y) for y in range(previous.year, target.year + 1)]
    if all(days is not None for days in calendars) and any(
        previous < d < target for days in calendars for d in days
    ):
        return pl.DataFrame(schema=schema)  # 缺前一交易日, 不能冒充单日涨幅
    frame = pl.read_parquet(paths[-1], columns=["symbol", "close"])
    frame = frame.with_columns(pl.col("close").cast(pl.Float64, strict=False))
    frame = frame.filter(pl.col("close").is_finite() & (pl.col("close") > 0))
    if frame.is_empty():
        return pl.DataFrame(schema=schema)
    pairs = pl.concat([
        frame.select("symbol").with_columns(
            pl.lit(datetime.combine(day, time())).alias("datetime"),
        ) for day in (previous, target)
    ])
    ratios = compute_ratio_pairs(pairs, data_dir)
    before = ratios.filter(pl.col("_d") == previous).select(
        "symbol", pl.col("_ratio").alias("_before"),
    )
    after = ratios.filter(pl.col("_d") == target).select(
        "symbol", pl.col("_ratio").alias("_after"),
    )
    frame = frame.join(before, on="symbol").join(after, on="symbol")
    if not raw:
        frame = frame.filter(
            ((pl.col("_before") - 1).abs() < 1e-12)
            & ((pl.col("_after") - 1).abs() < 1e-12)
        )
    return frame.select(
        "symbol", (pl.col("close") * pl.col("_before") / pl.col("_after")).alias("prev_close"),
    ).unique("symbol", keep="last")


def _rule(row: dict, target: date, raw: bool, calendar: frozenset[date] | None) -> tuple[str, float | None]:
    symbol = row["symbol"]
    if not is_current_a_share(symbol) or not row["_pit_known"]:
        return "unknown", None
    if not row["_pit_is_listed"]:
        return "unlisted", None
    listing = row["_pit_listing_date"]
    if not raw or listing is None or target < listing or calendar is None:
        return "unknown", None
    age = (target - listing).days
    if age <= no_limit_window_margin_days(symbol):
        if no_limit_window_days(symbol, listing) == 0 or listing.year != target.year:
            return "unknown", None  # 旧发行制度首日/缺日历, 不套普通涨跌停
        rank = sum(listing <= day <= target for day in calendar)
        if is_no_limit_day(symbol, listing, target, day_rank=rank):
            return "unlimited", None
    return "bounded", price_limit_pct(
        symbol, target, is_risk_warning=bool(row["_pit_is_risk_warning"]),
    )


def minute_returns(
    data_dir: Path, target_text: str,
) -> tuple[pl.DataFrame | None, str, bool, dict]:
    """Read one stock partition, retaining rejected timestamps with null returns.

    Null returns are excluded from averages and must not be forward-filled.
    Unknown rules remain visible and counted; they are not certified valid.
    """
    quality = {"total_rows": 0, "valid_rows": 0, "excluded_rows": 0,
               "unknown_rule_rows": 0, "unlimited_rows": 0}
    path = data_dir / "kline_minute" / f"date={target_text}" / "part.parquet"
    try:
        columns = pl.read_parquet_schema(path)
        has_amount = "amount" in columns
        bars = pl.read_parquet(
            path, columns=["symbol", "datetime", "close"] + (["amount"] if has_amount else []),
        ).with_columns(pl.col("close").cast(pl.Float64, strict=False))
        target = date.fromisoformat(target_text)
        quality["total_rows"] = bars.height
        bars = bars.filter(pl.col("datetime").dt.date() == target)
    except Exception:
        logger.warning("sector minute partition unreadable", exc_info=True)
        return None, "minute_schema", False, quality
    if bars.is_empty():
        quality["excluded_rows"] = quality["total_rows"]
        return None, "minute_empty", has_amount, quality

    raw = minute_basis_is_raw(data_dir)
    try:
        previous = _previous_close(data_dir, target, raw)
        states = attach_instrument_history(
            bars.select("symbol").unique().with_columns(pl.lit(target).alias("date")),
            load_instrument_history(data_dir),
        )
    except Exception:
        logger.warning("sector minute reference unavailable", exc_info=True)
        return None, "minute_reference", has_amount, quality
    calendar = published_trading_days(target.year)
    rules = [_rule(row, target, raw, calendar) for row in states.iter_rows(named=True)]
    states = states.select("symbol").with_columns(
        pl.Series("_rule", [r[0] for r in rules], dtype=pl.String),
        pl.Series("_limit", [r[1] for r in rules], dtype=pl.Float64),
    )
    refs = (
        bars.filter(pl.col("close").is_finite() & (pl.col("close") > 0))
        .group_by("symbol").agg(pl.col("close").sort_by("datetime").first().alias("_first"))
        .join(previous, on="symbol", how="left")
        .with_columns(pl.coalesce("prev_close", "_first").alias("_ref"))
    )
    n_prev = refs["prev_close"].is_not_null().sum()
    basis = "prev_close" if n_prev == refs.height else ("first_close" if n_prev == 0 else "mixed")
    bars = bars.join(refs, on="symbol", how="left").join(states, on="symbol", how="left")
    bars = bars.with_columns(
        pl.col("symbol").str.strip_chars().str.split(".").list.first().alias("_bare"),
        (pl.col("close") / pl.col("_ref") - 1).alias("_pct"),
        # 原始口径前收有效时才可执行交易所价格限制; 首分钟基准不能代替昨收。
        ((pl.col("_rule") == "bounded") & pl.col("prev_close").is_not_null()
         & pl.col("prev_close").is_finite() & (pl.col("prev_close") > 0)).alias("_bounded"),
    )
    limit_reference = pl.when(pl.col("_bounded")).then(pl.col("prev_close")).otherwise(None)
    bad_limit = pl.col("_bounded") & (
        (pl.col("close") > polars_limit_price(limit_reference, pl.col("_limit"), up=True) + 1e-8)
        | (pl.col("close") < polars_limit_price(limit_reference, pl.col("_limit"), up=False) - 1e-8)
    )
    valid = (
        pl.col("close").is_finite() & (pl.col("close") > 0)
        & pl.col("_pct").is_finite() & (pl.col("_ref") > 0)
        & (pl.col("_rule") != "unlisted") & ~bad_limit.fill_null(False)
        & pl.lit(calendar is None or target in calendar)
    ).fill_null(False)
    bars = bars.with_columns(
        (~valid).alias("_excluded"),
        pl.when(valid).then(pl.col("_pct")).otherwise(None).alias("_pct"),
        ((pl.col("_rule") == "unknown")
         | ((pl.col("_rule") == "bounded") & ~pl.col("_bounded"))).alias("_unknown"),
    )
    if has_amount:
        bars = bars.with_columns(
            pl.when(~pl.col("_excluded")).then(pl.col("amount")).otherwise(None).alias("amount"),
        )
    quality.update(
        valid_rows=bars["_pct"].is_not_null().sum(),
        excluded_rows=quality["total_rows"] - bars["_pct"].is_not_null().sum(),
        unknown_rule_rows=bars["_unknown"].sum(),
        unlimited_rows=(bars["_rule"] == "unlimited").sum(),
    )
    return bars, basis, has_amount, quality
