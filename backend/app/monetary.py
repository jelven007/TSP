"""Absolute CNY tolerance for completed-result reconciliation, never arithmetic."""
from decimal import Decimal, InvalidOperation, localcontext

import polars as pl

CNY_RESULT_TOLERANCE = Decimal("0.01")


def money_result_equal(left, right) -> bool:
    """Classify two finite yuan results without rounding or modifying either."""
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        if not a.is_finite() or not b.is_finite():
            return False
        with localcontext() as context:
            context.prec = max(34, len(a.as_tuple().digits), len(b.as_tuple().digits)) + abs(a.adjusted() - b.adjusted()) + 2
            return abs(a - b) <= CNY_RESULT_TOLERANCE
    except (InvalidOperation, ValueError, TypeError):
        return False


def money_result_equal_expr(left: pl.Expr, right: pl.Expr) -> pl.Expr:
    """Vectorized result check; invalid/overflow values cannot pass as money."""
    # Decimal conversion is local to the predicate; original columns are untouched.
    decimal = pl.Decimal(precision=38, scale=18)
    a = left.cast(pl.String).cast(decimal, strict=False)
    b = right.cast(pl.String).cast(decimal, strict=False)
    return ((a - b).abs() <= pl.lit(CNY_RESULT_TOLERANCE)).fill_null(False)
