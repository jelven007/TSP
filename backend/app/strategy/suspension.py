"""Dated suspension evidence; missing quotes and zero volume remain unknown."""
from datetime import date

import polars as pl

COLUMN = "_pool_suspended"


def annotate(rows: pl.DataFrame, *, as_of: date | None = None) -> pl.DataFrame:
    if rows.is_empty() or COLUMN in rows.columns:
        return rows
    day_field = next((name for name in ("date", "datetime", "last_datetime") if name in rows.columns), None)
    day = pl.col(day_field).cast(pl.Date) if day_field else None
    if day is None:
        return rows.with_columns(pl.lit(None, dtype=pl.Boolean).alias(COLUMN))
    dated = day.is_not_null() if as_of is None else day == as_of
    state = (pl.col("is_suspended").cast(pl.Boolean) if "is_suspended" in rows.columns
             else pl.lit(None, dtype=pl.Boolean))
    # A real trade establishes that the stock was trading. No trade does not
    # establish a halt (before open, missing input, illiquidity are distinct).
    if "volume" in rows.columns:
        state = state.fill_null(pl.when(pl.col("volume") > 0).then(False).otherwise(None))
    return rows.with_columns(pl.when(dated).then(state).otherwise(None).alias(COLUMN))
