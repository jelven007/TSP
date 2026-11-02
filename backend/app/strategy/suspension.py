"""Dated suspension evidence; missing quotes and zero volume remain unknown."""
from datetime import date

import polars as pl

COLUMN = "_pool_suspended"


def annotate(rows: pl.DataFrame, *, as_of: date | None = None,
             evidence: pl.DataFrame | None = None) -> pl.DataFrame:
    from app.instrument_suspension import DATE_COLUMN, STATUS_COLUMN

    result = _annotate_quotes(rows, as_of=as_of)
    if (result.is_empty() or evidence is None or evidence.is_empty() or as_of is None
            or not {DATE_COLUMN, STATUS_COLUMN, "symbol"}.issubset(evidence.columns)):
        return result
    status = evidence.filter(pl.col(DATE_COLUMN) == as_of).select(
        "symbol", pl.col(STATUS_COLUMN).alias("_dated_halt"),
    ).unique("symbol", keep="last")
    return result.join(status, on="symbol", how="left").with_columns(
        pl.col(COLUMN).fill_null(pl.col("_dated_halt")).alias(COLUMN),
    ).drop("_dated_halt")


def _annotate_quotes(rows: pl.DataFrame, *, as_of: date | None = None) -> pl.DataFrame:
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
