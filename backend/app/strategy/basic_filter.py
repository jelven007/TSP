"""Numeric filter reference values, separate from signal and execution prices."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import polars as pl

from app.market_time import published_trading_day
from app.strategy.eligibility import new_stock_min_days

PREFIX = "_bf_previous_"
RANGES = {
    "price": "raw_close", "market_cap": "total_shares", "float_cap": "float_shares",
    "amount": "amount", "turnover": "turnover_rate", "change_pct": "change_pct",
}


def fields(config: dict) -> set[str]:
    if not config.get("enabled", True):
        return set()
    result = {field for key, field in RANGES.items()
              if any(config.get(f"{key}_{side}") is not None for side in ("min", "max"))}
    if result.intersection({"total_shares", "float_shares"}):
        result.add("raw_close")
    if new_stock_min_days(config):
        result.add("listing_age_days")
    return result


def uses_previous(config: dict) -> bool:
    return config.get("data_reference") == "previous_trade_day" and bool(fields(config))


@lru_cache(maxsize=4096)
def previous_session(day: date) -> date:
    prior = day - timedelta(days=1)
    while True:
        known = published_trading_day(prior)
        if known is None:
            raise ValueError(f"{prior}缺少已发布交易日历")
        if known:
            return prior
        prior -= timedelta(days=1)


def attach_previous_values(
    rows: pl.DataFrame, config: dict, *, history: pl.DataFrame | None = None, as_of: date | None = None,
) -> pl.DataFrame:
    """Exact session join; no asof join, stale fill, or current-price replacement."""
    wanted = fields(config)
    if not uses_previous(config) or rows.is_empty():
        return rows
    source = history if history is not None else rows
    columns = {PREFIX + field for field in wanted}
    if columns <= set(rows.columns):
        return rows
    if as_of is not None:
        dates = pl.Series("_bf_date", [as_of] * rows.height, dtype=pl.Date)
    elif "date" in rows.columns:
        dates = rows["date"].cast(pl.Date)
    elif "datetime" in rows.columns:
        dates = rows["datetime"].cast(pl.Date)
    else:
        return rows.with_columns(pl.lit(None, dtype=pl.Float64).alias(name) for name in sorted(columns))
    unique = dates.unique().to_list()
    mapping = {day: previous_session(day) for day in unique if day is not None}
    left = rows.with_columns(pl.Series("_bf_date", [mapping.get(day) for day in dates], dtype=pl.Date))
    if source.is_empty() or not {"symbol", "date"} <= set(source.columns):
        return left.drop("_bf_date").with_columns(
            pl.lit(None, dtype=pl.Float64).alias(name) for name in sorted(columns))
    right = source.select(
        "symbol", pl.col("date").cast(pl.Date).alias("_bf_date"),
        *[(pl.col("close") if field == "raw_close" and "raw_close" not in source.columns
           and "close" in source.columns else pl.col(field) if field in source.columns
           else pl.lit(None, dtype=pl.Float64)).alias(PREFIX + field) for field in sorted(wanted)],
    ).unique(["symbol", "_bf_date"], keep="last")
    return left.join(right, on=["symbol", "_bf_date"], how="left", maintain_order="left").drop("_bf_date")


def numeric_expression(rows: pl.DataFrame, config: dict) -> pl.Expr | None:
    def col(field):
        name = PREFIX + field if uses_previous(config) else field
        if name not in rows.columns and field == "raw_close" and not uses_previous(config):
            name = "close"
        return pl.col(name).cast(pl.Float64) if name in rows.columns else pl.lit(None, dtype=pl.Float64)

    result = []
    price = col("raw_close")
    for stem, field in RANGES.items():
        low, high = config.get(stem + "_min"), config.get(stem + "_max")
        if low is None and high is None:
            continue
        values = col(field)
        result.append(values.is_finite())
        if stem in {"price", "market_cap", "float_cap"}:
            result.append(values > 0)
        if stem in {"market_cap", "float_cap"}:
            result.append(price.is_finite() & (price > 0))
            values = price * values
        elif stem == "change_pct":
            values = values * 100
        if low is not None:
            result.append(values >= low)
        if high is not None:
            result.append(values <= high)
    if new_stock_min_days(config):
        result.append(col("listing_age_days") >= new_stock_min_days(config))
    return pl.all_horizontal(result).fill_null(False) if result else None
