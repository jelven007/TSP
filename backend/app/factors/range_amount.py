"""Exact raw day range/amount score; prices share a basis, amount is yuan."""
from __future__ import annotations

import numpy as np
import polars as pl

FACTOR_ID = "intraday_range_amount_score"


def range_amount_expr(
    high: pl.Expr, low: pl.Expr, previous_close: pl.Expr, amount: pl.Expr,
) -> pl.Expr:
    high, low, previous_close, amount = [
        value.cast(pl.Float64) for value in (high, low, previous_close, amount)
    ]
    valid = (
        pl.all_horizontal([value.is_finite() for value in (high, low, previous_close, amount)])
        & (high >= low) & (low > 0) & (previous_close > 0) & (amount > 0)
    )
    return pl.when(valid).then(-(high - low) / previous_close + .001 * amount.log()).otherwise(None)


def range_amount_arrays(high, low, previous_close, amount) -> np.ndarray:
    high, low, previous_close, amount = np.broadcast_arrays(
        *[np.asarray(value, dtype=np.float64) for value in (high, low, previous_close, amount)]
    )
    valid = (
        np.isfinite(high) & np.isfinite(low) & np.isfinite(previous_close) & np.isfinite(amount)
        & (high >= low) & (low > 0) & (previous_close > 0) & (amount > 0)
    )
    result = np.full(high.shape, np.nan, dtype=np.float64)
    result[valid] = -(high[valid] - low[valid]) / previous_close[valid] + .001 * np.log(amount[valid])
    return result
